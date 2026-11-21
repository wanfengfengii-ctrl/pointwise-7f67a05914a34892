"""Interleave real station calls and an already prepared recorder command.

The module demonstrates three handoffs against one SQLite station:

* ``trace_interleave`` — the original workday regression: an unrelated
  recording keeps publishing while a prepared overwrite waits, the device
  writes with a lost reply, and the station settlement is rejected once.
* ``trace_cancelled`` — the device terminal is a withdrawal: the late command
  must not touch the slot and the old ticket still delivers the original.
* ``trace_executed`` — the device wrote before the withdrawal request, so the
  request reports ``executed`` and the original poll number resumes to edition
  two with the actual new occurrence.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from survey.capture.candidates import CandidateFrame, OverwriteRequest, RecordingEvent
from survey.device.readout import frame_checksum
from survey.device.recorder import OverwriteReplyLost, OverwriteWithdrawn
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


def _page(page):
    return [frame.amplitude for frame in page.frames]


def _sources(page):
    return [list(source.fields()) for source in page.sources]


def _audit(audit):
    return {"peak": audit.frozen.peak, "source": list(audit.frozen.source.fields()), "matches": audit.matches}


def trace_interleave(path):
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
        if committed is None or station.recorder.read_occurrence(
                "b", committed.replacement.occurrence_id).amplitude != 19:
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
        if (pending.status, before_close.status, closed.status, station.recorder.connected) != (
                "pending", "pending", "closed", False,
        ) or _page(old_page) != [11] or _page(new_page) != [7, 19] \
                or not (old_audit.matches and new_audit.matches):
            raise AssertionError("fixture lost the original handoff or the committed replacement")
        return {
            "alpha_while_paused": pending.status,
            "unrelated_beta": beta.status,
            "unrelated_delta": delta.status,
            "cross_recording_gamma": gamma.status,
            "close_before": before_close.status,
            "device_occurrence": committed.replacement.occurrence_id,
            "publication_rollback": cut.fired,
            "alpha_edition": resumed.edition,
            "old_tail": _page(old_page), "old_sources": _sources(old_page),
            "new_values": _page(new_page), "new_sources": _sources(new_page),
            "peaks": [old_audit.frozen.peak, new_audit.frozen.peak],
            "close_after": closed.status,
        }


def trace_cancelled(path):
    with connect_station(path) as station:
        shift = seed(station)
        event = shift.alpha_event
        pending = station.poll("alpha", event, "alpha-swap", pause_before_overwrite=True)
        command = prepared_command(station, "alpha-swap", event)
        close_while_pending = station.close("alpha", "end-shift")
        cancelled = station.cancel_overwrite("alpha", "alpha-swap")
        late_blocked = False
        try:
            command.send(station)
        except OverwriteWithdrawn:
            late_blocked = True
        replay = station.poll("alpha", event, "alpha-swap")
        edition_after = station.current_edition("alpha").version
        count_after = station.recorder.overwrite_count()
        live_b = station.recorder.read_occurrence("b", shift.b.occurrence_id).amplitude
        closed = station.close("alpha", "end-shift")

    with connect_station(path) as station:
        replay_after = station.poll("alpha", event, "alpha-swap")
        reclaimed = station.cancel_overwrite("alpha", "alpha-swap")
        old_page = station.take(shift.old_ticket, "old-tail", 2)
        old_audit = station.audit(shift.old_ticket)
        if (pending.status, close_while_pending.status, cancelled.status, cancelled.edition,
                replay.status, late_blocked, edition_after, count_after, live_b, closed.status,
                replay_after.status, reclaimed.status) != (
                "pending", "pending", "cancelled", None, "cancelled", True, 1, 0, 11, "closed",
                "cancelled", "cancelled") or _page(old_page) != [11] or not old_audit.matches:
            raise AssertionError("withdrawal branch broke the original handoff")
        return {
            "alpha_while_paused": pending.status,
            "close_while_pending": close_while_pending.status,
            "cancel": [cancelled.status, cancelled.edition],
            "late_command_blocked": late_blocked,
            "poll_replay": replay.status,
            "edition_after": edition_after,
            "overwrite_count": count_after,
            "live_b": live_b,
            "close_after_cancel": closed.status,
            "reopen_poll": replay_after.status,
            "reopen_cancel": reclaimed.status,
            "old_tail": _page(old_page), "old_sources": _sources(old_page),
            "old_audit": _audit(old_audit),
        }


def trace_executed(path):
    with connect_station(path) as station:
        shift = seed(station)
        event = shift.alpha_event
        lost = station.poll("alpha", event, "alpha-swap", lose_overwrite_reply=True)
        written = station.recorder.operation_outcome("alpha-swap")
        with reject_write(station) as cut:
            try:
                station.poll("alpha", event, "alpha-swap")
            except CommitRejected:
                pass
        edition_still = station.current_edition("alpha").version
        issue_blocked = False
        try:
            station.issue("alpha", 2, ALPHA_WINDOW, "new-ticket")
        except LookupError:
            issue_blocked = True
        close_while_pending = station.close("alpha", "end-shift")

    with connect_station(path) as station:
        executed = station.cancel_overwrite("alpha", "alpha-swap")
        resumed = station.poll("alpha", event, "alpha-swap")
        again = station.poll("alpha", event, "alpha-swap")
        count = station.recorder.overwrite_count()
        old_page = station.take(shift.old_ticket, "old-tail", 2)
        old_audit = station.audit(shift.old_ticket)
        new = station.issue("alpha", resumed.edition, ALPHA_WINDOW, "new-ticket")
        new_page = station.take(new.ticket_id, "new-page", 2)
        new_audit = station.audit(new.ticket_id)
        closed = station.close("alpha", "end-shift")

    with connect_station(path) as station:
        reopened_old = station.audit(shift.old_ticket)
        reopened_new = station.audit(new.ticket_id)
        if not (reopened_old.matches and reopened_new.matches):
            raise AssertionError("frozen peaks no longer reconcile after reopening")
        return {
            "reply_lost": lost.status,
            "device_occurrence": written.replacement.occurrence_id,
            "device_sequence": written.replacement.sequence,
            "publication_rollback": cut.fired,
            "edition_still": edition_still,
            "issue_blocked": issue_blocked,
            "close_while_pending": close_while_pending.status,
            "cancel": [executed.status, executed.edition],
            "resumed_edition": resumed.edition,
            "replay_same": again == resumed,
            "overwrite_count": count,
            "old_tail": _page(old_page), "old_sources": _sources(old_page),
            "new_values": _page(new_page), "new_sources": _sources(new_page),
            "new_peak_source": list(new_audit.frozen.source.fields()),
            "old_audit": _audit(reopened_old), "new_audit": _audit(reopened_new),
            "close_after": closed.status,
        }


def run_all(directory):
    directory = Path(directory)
    return {
        "interleave": trace_interleave(directory / "interleave.sqlite"),
        "cancelled": trace_cancelled(directory / "cancelled.sqlite"),
        "executed": trace_executed(directory / "executed.sqlite"),
    }


def verify():
    with TemporaryDirectory(prefix="overwrite-interleave-") as directory:
        result = run_all(directory)
    expected = {
        "interleave": {
            "alpha_while_paused": "pending", "unrelated_beta": "published",
            "unrelated_delta": "published", "cross_recording_gamma": "published",
            "close_before": "pending", "publication_rollback": True, "alpha_edition": 2,
            "old_tail": [11], "new_values": [7, 19], "peaks": [11, 19], "close_after": "closed",
        },
        "cancelled": {
            "alpha_while_paused": "pending", "close_while_pending": "pending",
            "cancel": ["cancelled", None], "late_command_blocked": True,
            "poll_replay": "cancelled", "edition_after": 1, "overwrite_count": 0, "live_b": 11,
            "close_after_cancel": "closed", "reopen_poll": "cancelled", "reopen_cancel": "cancelled",
            "old_tail": [11],
        },
        "executed": {
            "reply_lost": "pending", "publication_rollback": True, "edition_still": 1,
            "issue_blocked": True, "close_while_pending": "pending",
            "cancel": ["executed", None], "resumed_edition": 2, "replay_same": True,
            "overwrite_count": 1, "old_tail": [11], "new_values": [7, 19], "close_after": "closed",
        },
    }
    for key, fields in expected.items():
        for field, value in fields.items():
            actual = result[key][field]
            if actual != value:
                raise AssertionError(f"{key}.{field} = {actual!r}, expected {value!r}")
    if result["executed"]["new_peak_source"] != [
            result["executed"]["device_occurrence"], result["executed"]["device_sequence"], "b"]:
        raise AssertionError("new edition peak did not point at the written occurrence")
    if result["interleave"]["old_sources"] != [["frame-3", 3, "b"]]:
        raise AssertionError("old ticket did not keep the original b occurrence")
    if result["cancelled"]["old_sources"] != [["frame-3", 3, "b"]]:
        raise AssertionError("withdrawal branch changed the old source")
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Interleave overwrite and withdrawal handoffs")
    parser.add_argument("command", nargs="?", choices=("demo", "verify"), default="demo")
    arguments = parser.parse_args(argv)
    if arguments.command == "verify":
        verify()
        print("OVERWRITE_INTERLEAVE_OK")
        return 0
    with TemporaryDirectory(prefix="overwrite-interleave-") as directory:
        result = run_all(directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
