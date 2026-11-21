import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from survey.capture.accept import PollResult, cancel_overwrite, poll
from survey.capture.candidates import CandidateFrame, OverwriteRequest, PollJournal, RecordingEvent
from survey.capture.custody import CustodyStore
from survey.capture.frontier import AcceptedEdition, EditionFrontier
from survey.device.readout import frame_checksum
from survey.device.recorder import Recorder
from survey.measurement.amplitude import RAW_COUNT_UNIT
from survey.measurement.peak import MeasurementStore
from survey.measurement.reconcile import AuditResult, audit
from survey.recording.coordinates import FrameWindow, require_name
from survey.recording.lifecycle import CloseResult, OpenResult, close, install_lifecycle_schema, open
from survey.recording.plan import install_plan_schema
from survey.recording.requests import RecordingRequests
from survey.recording.transactions import StationDatabase
from survey.tickets.delivery import DeliveryPage, take
from survey.tickets.issue import IssuedTicket, issue
from survey.tickets.members import TicketMembers
from survey.tickets.progress import TicketProgress
from survey.tickets.receipts import ReceiptBook


class Station:
    def __init__(self, db: StationDatabase):
        self.db = db
        conn = db.connection
        self.requests = RecordingRequests(conn)
        self.recorder = Recorder(db)
        self.journal = PollJournal(conn)
        self.frontier = EditionFrontier(conn)
        self.custody = CustodyStore(conn, self.recorder)
        self.members = TicketMembers(conn)
        self.progress = TicketProgress(conn)
        self.receipts = ReceiptBook(conn)
        self.measurements = MeasurementStore(conn)

    def open(self, recording: str, window: FrameWindow, unit: str = RAW_COUNT_UNIT) -> OpenResult:
        return open(self, recording, window, unit)

    def poll(self, recording: str, event: RecordingEvent, operation: str, *,
             pause_before_overwrite: bool = False, lose_overwrite_reply: bool = False,
             fail_custody_commit: bool = False) -> PollResult:
        return poll(self, recording, event, operation, pause_before_overwrite=pause_before_overwrite,
                    lose_overwrite_reply=lose_overwrite_reply, fail_custody_commit=fail_custody_commit)

    def cancel_overwrite(self, recording: str, operation: str) -> PollResult:
        return cancel_overwrite(self, recording, operation)

    def issue(self, recording: str, edition: int, window: FrameWindow, operation: str) -> IssuedTicket:
        return issue(self, recording, edition, window, operation)

    def take(self, ticket_id: int, operation: str, limit: int) -> DeliveryPage:
        return take(self, ticket_id, operation, limit)

    def close(self, recording: str, operation: str) -> CloseResult:
        return close(self, recording, operation)

    def audit(self, ticket_id: int) -> AuditResult:
        return audit(self, ticket_id)

    def current_edition(self, recording: str) -> AcceptedEdition:
        require_name(recording)
        with self.db.atomic():
            return self.frontier.load(recording)

    def dispose(self) -> None:
        self.db.disconnect()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.dispose()


def connect_station(path) -> Station:
    db = StationDatabase(path)
    try:
        db.initialize((install_plan_schema, RecordingRequests.install, install_lifecycle_schema,
                       Recorder.install, PollJournal.install, CustodyStore.install, EditionFrontier.install,
                       TicketMembers.install, TicketProgress.install, ReceiptBook.install, MeasurementStore.install))
    except BaseException:
        db.disconnect()
        raise
    return Station(db)


def _page_output(page: DeliveryPage) -> dict:
    return {'values': [frame.amplitude for frame in page.frames],
            'sources': [list(source.fields()) for source in page.sources],
            'positions': [page.start, page.next_position]}


def _audit_output(result: AuditResult) -> dict:
    measurement = result.frozen
    return {'count': measurement.count, 'peak': measurement.peak,
            'peak_source': list(measurement.source.fields()) if measurement.source else None,
            'matches': result.matches}


def run_workday(db_path, fixture: dict) -> dict:
    recording = fixture['recording']
    window = FrameWindow(*fixture['window'])
    with connect_station(db_path) as station:
        station.open(recording, window)
        first_frames = tuple(CandidateFrame(position, station.recorder.occupy(item['slot'], item['amplitude']))
                             for position, item in zip(window.positions(), fixture['samples'], strict=True))
        initial = station.poll(recording, RecordingEvent(first_frames), 'morning-capture')
        old = station.issue(recording, initial.edition, window, 'night-ticket')
        prefix = station.take(old.ticket_id, 'night-first', 1)
        replacement_spec = fixture['replacement']
        last = first_frames[-1]
        amplitude = replacement_spec['amplitude']
        request = OverwriteRequest(last.position, last.frame.slot, last.frame.occurrence_id,
                                   amplitude, frame_checksum(amplitude))
        event = RecordingEvent(first_frames[:-1], request)
        updated = station.poll(recording, event, 'morning-overwrite')
        new = station.issue(recording, updated.edition, window, 'day-ticket')
        closed = station.close(recording, 'finish-workday')
    with connect_station(db_path) as station:
        suffix = station.take(old.ticket_id, 'day-continue-night', window.length)
        entire_new = station.take(new.ticket_id, 'day-review', window.length)
        audits = [_audit_output(station.audit(ticket.ticket_id)) for ticket in (old, new)]
        return {'pages': [_page_output(page) for page in (prefix, suffix, entire_new)],
                'audits': audits, 'closed': closed.status, 'connected_after_reopen': station.recorder.connected}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Acoustic survey station')
    parser.add_argument('command', choices=('demo',))
    parser.parse_args(argv)
    sample = Path(__file__).resolve().parents[2] / 'examples' / 'workday.json'
    fixture = json.loads(sample.read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='survey-workday-') as directory:
        result = run_workday(Path(directory) / 'station.sqlite', fixture)
    expected = [
        {'values': [7], 'sources': [['frame-1', 1, 'a']], 'positions': [0, 1]},
        {'values': [11], 'sources': [['frame-2', 2, 'b']], 'positions': [1, 2]},
        {'values': [7, 19], 'sources': [['frame-1', 1, 'a'], ['frame-3', 3, 'b']], 'positions': [0, 2]},
    ]
    expected_audits = [
        {'count': 2, 'peak': 11, 'peak_source': ['frame-2', 2, 'b'], 'matches': True},
        {'count': 2, 'peak': 19, 'peak_source': ['frame-3', 3, 'b'], 'matches': True},
    ]
    downstream_expected = {'audits': expected_audits, 'closed': 'closed', 'connected_after_reopen': False}
    downstream = {key: result[key] for key in downstream_expected}
    if result['pages'] != expected or downstream != downstream_expected:
        raise AssertionError(f'workday handoff differs from the sample ledger: {result!r}')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    paths = ['survey/capture/accept.py', 'survey/capture/overwrite.py', 'survey/capture/custody.py',
             'survey/tickets/members.py', 'survey/tickets/delivery.py', 'survey/measurement/reconcile.py']
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'v3.frozen_members', 'input': fixture, 'expected': expected, 'actual': result['pages'],
        'downstream_expected': downstream_expected, 'downstream_actual': downstream, 'code_paths': paths,
        'oracle': 'Hand ledger keeps device occurrences 1 and 2 on the old ticket and 1 and 3 on the new ticket. '
                  'Their absolute maxima are 11 at occurrence 2 and 19 at occurrence 3.',
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
