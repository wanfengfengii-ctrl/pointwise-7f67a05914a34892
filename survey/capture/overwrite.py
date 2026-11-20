from survey.capture.candidates import OverwriteRequest, PollJournal, PollRecord
from survey.capture.custody import CustodyStore
from survey.capture.frontier import EditionFrontier
from survey.device.operations import OverwriteOutcome
from survey.device.recorder import Recorder
from survey.recording.transactions import StationDatabase


def prepare_overwrite(db: StationDatabase, journal: PollJournal, frontier: EditionFrontier,
                      custody: CustodyStore, operation: str, *, reject_commit: bool = False) -> PollRecord:
    with db.atomic():
        record = journal.load(operation)
        if record is None or record.expected is None:
            raise RuntimeError('no overwrite to prepare')
        if record.state != 'staged':
            return record
        expected = record.expected
        if record.keeps_expected or expected in frontier.referenced_occurrences(expected.slot):
            custody.preserve(expected)
        journal.mark_prepared(operation)
        if reject_commit:
            from survey.recording.transactions import CommitRejected
            raise CommitRejected('custody transaction rejected before commit')
    return journal.load(operation)


def apply_overwrite(recorder: Recorder, record: PollRecord, request: OverwriteRequest,
                    *, lose_reply: bool = False) -> OverwriteOutcome:
    if record.state != 'prepared' or record.expected is None:
        raise RuntimeError('overwrite has no committed preparation')
    return recorder.overwrite(record.operation, record.expected, request.amplitude,
                              request.checksum, lose_reply=lose_reply)


def recover_overwrite(recorder: Recorder, record: PollRecord, request: OverwriteRequest,
                      *, lose_reply: bool = False) -> OverwriteOutcome:
    outcome = recorder.operation_outcome(record.operation)
    if outcome is not None:
        recorder.check_outcome(outcome, record.expected, request.checksum)
        return outcome
    return apply_overwrite(recorder, record, request, lose_reply=lose_reply)


def has_pending_overwrites(journal: PollJournal) -> bool:
    return bool(journal.pending_overwrites())
