"""Interleave real station calls and an already prepared recorder command."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from survey.capture.candidates import CandidateFrame, OverwriteRequest, RecordingEvent
from survey.device.readout import frame_checksum
from survey.device.recorder import OverwriteReplyLost
from survey.device.slots import FrameOccurrence, FrameRef
from survey.recording.coordinates import FrameWindow
from survey.recording.station import connect_station
from survey.recording.transactions import CommitRejected


ALPHA_WINDOW = FrameWindow(0, 2)
ONE_FRAME = FrameWindow(0, 1)


def direct(*frames):
    return RecordingEvent(tuple(CandidateFrame(pos, frame) for pos, frame in enumerate(frames)))


def swap(old, amplitude, *prefix):
    request = OverwriteRequest(len(prefix), old.slot, old.occurrence_id, amplitude, frame_checksum(amplitude))
    return RecordingEvent(direct(*prefix).frames, request)


@dataclass(frozen=True)
class Shift:
    a: FrameOccurrence
    b: FrameOccurrence
    c: FrameOccurrence
    d: FrameOccurrence
    old_ticket: int

    @property
    def alpha_event(self):
        return swap(self.b, 19, self.a)


def seed(station):
    station.open("alpha", ALPHA_WINDOW)
    for recording in ("beta", "gamma", "delta"):
        station.open(recording, ONE_FRAME)
    p, a, b, c, d = (station.recorder.occupy(slot, value)
                     for slot, value in (("p", 5), ("a", 7), ("b", 11), ("c", 29), ("d", 31)))
    initial = station.poll("alpha", direct(p, b), "alpha-first")
    ticket = station.issue("alpha", initial.edition, ALPHA_WINDOW, "old-ticket")
    station.take(ticket.ticket_id, "old-prefix", 1)
    return Shift(a, b, c, d, ticket.ticket_id)


@dataclass(frozen=True)
class DeviceCommand:
    operation: str
    expected: FrameRef
    amplitude: int
    checksum: str

    def send(self, station, *, lose_reply=False):
        return station.recorder.overwrite(self.operation, self.expected, self.amplitude,
                                          self.checksum, lose_reply=lose_reply)


def prepared_command(station, operation, event):
    record = station.journal.load(operation)
    if record is None or record.state != "prepared" or record.expected is None or event.overwrite is None:
        raise ValueError("pause a prepared overwrite before saving its device command")
    record.check_request(record.recording, event.manifest())
    return DeviceCommand(operation, record.expected, event.overwrite.amplitude, event.overwrite.checksum)


@dataclass
class CommitCut:
    fired: bool = False


@contextmanager
def reject_write(station, ordinal=1):
    """Reject the selected writing transaction at its real commit boundary."""
    if ordinal < 1:
        raise ValueError("write ordinal must be positive")
    original = station.db.atomic
    cut = CommitCut()
    writes = 0

    @contextmanager
    def atomic(*args, **kwargs):
        nonlocal writes
        with original(*args, **kwargs) as conn:
            before = conn.total_changes
            yield conn
            if conn.total_changes > before:
                writes += 1
                if writes == ordinal:
                    cut.fired = True
                    raise CommitRejected("fixture rejected this write transaction")

    with patch.object(station.db, "atomic", atomic):
        yield cut


def trace(path):
    with connect_station(path) as station:
        shift = seed(station)
        event = shift.alpha_event
        pending = station.poll("alpha", event, "alpha-swap", pause_before_overwrite=True)
        command = prepared_command(station, "alpha-swap", event)
        beta = station.poll("beta", direct(shift.c), "beta-read")
        delta = station.poll("delta", swap(shift.d, 37), "delta-swap")
        gamma = station.poll("gamma", swap(shift.a, 17), "gamma-swap")
        before_close = station.close("alpha", "end-shift")
        try:
            command.send(station, lose_reply=True)
        except OverwriteReplyLost:
            pass
        else:
            raise AssertionError("device did not simulate loss after its commit")

    with connect_station(path) as station:
        committed = station.recorder.operation_outcome(command.operation)
        if committed is None or station.recorder.read_occurrence("b", committed.replacement.occurrence_id).amplitude != 19:
            raise AssertionError("device change did not survive reopening")
        with reject_write(station) as cut:
            try:
                station.poll("alpha", event, command.operation)
            except CommitRejected:
                pass
        if not cut.fired or station.current_edition("alpha").version != 1:
            raise AssertionError("fixture did not reject publication before commit")

    with connect_station(path) as station:
        resumed = station.poll("alpha", event, command.operation)
        new = station.issue("alpha", resumed.edition, ALPHA_WINDOW, "new-ticket")
        closed = station.close("alpha", "end-shift")

    with connect_station(path) as station:
        old_page = station.take(shift.old_ticket, "old-tail", 2)
        new_page = station.take(new.ticket_id, "new-page", 2)
        old_audit = station.audit(shift.old_ticket)
        new_audit = station.audit(new.ticket_id)
        old_values = [frame.amplitude for frame in old_page.frames]
        new_values = [frame.amplitude for frame in new_page.frames]
        if (pending.status, before_close.status, closed.status, station.recorder.connected) != (
            "pending", "pending", "closed", False,
        ) or old_values != [11] or new_values != [7, 19] or not (old_audit.matches and new_audit.matches):
            raise AssertionError("fixture lost the original handoff or the committed replacement")
        return {"alpha_while_paused": pending.status, "beta": beta.status,
                "delta": delta.status, "gamma": gamma.status, "close_before": before_close.status,
                "device_occurrence": committed.replacement.occurrence_id,
                "publication_rollback": cut.fired, "alpha_edition": resumed.edition,
                "old_tail": old_values, "old_sources": [source.fields() for source in old_page.sources],
                "new_values": new_values, "new_sources": [source.fields() for source in new_page.sources],
                "peaks": [old_audit.frozen.peak, new_audit.frozen.peak], "close_after": closed.status}


def main():
    with TemporaryDirectory(prefix="overwrite-interleave-") as directory:
        result = trace(Path(directory) / "station.sqlite")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
