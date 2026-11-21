from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from survey.capture.candidates import CandidateFrame, RecordingEvent
from survey.capture.overwrite import (ObligationPending, heal_cancellations, prepare_overwrite,
                                      preserve_slot, recover_overwrite, withdraw_overwrite)
from survey.device.recorder import OverwriteReplyLost, OverwriteWithdrawn
from survey.device.slots import FrameUnavailable
from survey.recording.coordinates import require_name
from survey.recording.lifecycle import require_active_recording
from survey.recording.transactions import CommitRejected

if TYPE_CHECKING:
    from survey.recording.station import Station


@dataclass(frozen=True)
class PollResult:
    status: str
    edition: int | None = None
    reason: str | None = None


def _verify_frames(station: Station, frames: tuple[CandidateFrame, ...]) -> None:
    for item in frames:
        try:
            actual = station.custody.resolve(item.frame.reference)
        except FrameUnavailable as error:
            raise ValueError(f'candidate has unverifiable facts at position {item.position}') from error
        if actual != item.frame:
            raise ValueError(f'candidate body differs at position {item.position}')


def _publish(station: Station, recording: str, operation: str,
             frames: tuple[CandidateFrame, ...]) -> PollResult:
    edition = station.frontier.publish(recording, frames)
    station.journal.mark_published(operation, edition.version)
    return PollResult('published', edition.version)


def _slot_still_promised(station: Station, recording: str, replacement) -> bool:
    """Whether an unexecuted overwrite elsewhere still depends on the live target slot.

    A pending write that has not reached the device needs its expected frame to
    remain in the slot; another recording may overwrite the same physical slot
    only after that frame's identity, body and source are preserved.
    """
    if replacement is None:
        return False
    for pending in station.journal.pending_overwrites():
        if pending.recording == recording or pending.expected is None:
            continue
        if (pending.expected.slot == replacement.slot
                and station.recorder.operation_outcome(pending.operation) is None
                and station.recorder.cancellation_outcome(pending.operation) is None):
            return True
    return False


def poll(station: Station, recording: str, event: RecordingEvent, operation: str, *,
         pause_before_overwrite: bool = False, lose_overwrite_reply: bool = False,
         fail_custody_commit: bool = False) -> PollResult:
    require_name(recording)
    require_name(operation)
    manifest = event.manifest()
    replacement = event.overwrite
    with station.db.atomic():
        heal_cancellations(station.journal, station.recorder)
        withdrawn = station.journal.load_cancel(operation)
        if withdrawn is not None:
            withdrawn.check_request(recording, manifest)
            return PollResult('cancelled')
        record = station.journal.load(operation)
        if record is not None:
            record.check_request(recording, manifest)
            if record.state == 'published':
                return PollResult('published', record.edition)
        plan = require_active_recording(station, recording)
        if record is None:
            # A later overwrite in the same recording waits for the original to settle.
            if station.journal.pending_overwrites(recording=recording):
                return PollResult('pending', reason='another_overwrite')
            # Another recording may only displace a slot once an unexecuted
            # command for that same slot is no longer waiting on its occupant.
            if _slot_still_promised(station, recording, replacement):
                return PollResult('pending', reason='another_overwrite')
            position = None if replacement is None else replacement.position
            station.frontier.validate_positions(plan.window, event.frames, position)
            _verify_frames(station, event.frames)
            if replacement is None:
                station.journal.stage(operation, recording, manifest, None, False)
                return _publish(station, recording, operation, event.frames)
            expected = station.recorder.current_reference(replacement.slot, replacement.expected_occurrence)
            keeps_expected = any(item.frame.reference == expected for item in event.frames)
            station.journal.stage(operation, recording, manifest, expected, keeps_expected)

    try:
        record = prepare_overwrite(station.db, station.journal, station.frontier, station.custody,
                                   station.recorder, operation, reject_commit=fail_custody_commit)
    except CommitRejected:
        return PollResult('pending', reason='custody_commit_failed')
    except ObligationPending:
        return PollResult('pending', reason='another_overwrite')
    if record.state == 'published':
        return PollResult('published', record.edition)
    if pause_before_overwrite:
        return PollResult('pending', reason='paused_before_overwrite')
    try:
        # Candidates added after preparation (an edition published meanwhile)
        # must not lose their originals to this already-cleared overwrite.
        with station.db.atomic():
            if station.journal.load(operation) is None:
                return PollResult('cancelled')
            preserve_slot(station.custody, station.journal, station.frontier,
                          station.recorder, record.expected.slot)
    except ObligationPending:
        return PollResult('pending', reason='another_overwrite')
    try:
        outcome = recover_overwrite(station.recorder, record, replacement, lose_reply=lose_overwrite_reply)
    except OverwriteReplyLost:
        return PollResult('pending', reason='overwrite_reply_lost')
    except OverwriteWithdrawn:
        return PollResult('cancelled')

    with station.db.atomic():
        record = station.journal.load(operation)
        if record is None:
            if station.journal.load_cancel(operation) is not None:
                return PollResult('cancelled')
            raise LookupError(f'unknown poll operation {operation!r}')
        if record.state == 'published':
            return PollResult('published', record.edition)
        plan = require_active_recording(station, recording)
        new_frame = station.custody.resolve(outcome.replacement)
        frames = (*event.frames, CandidateFrame(replacement.position, new_frame))
        station.frontier.validate_positions(plan.window, frames)
        _verify_frames(station, frames)
        return _publish(station, recording, operation, frames)


def cancel_overwrite(station: Station, recording: str, operation: str) -> PollResult:
    """Withdraw a registered overwrite before its device write is accepted.

    The operation number is the original ``poll`` number.  The outcome follows
    what the device actually did: a withdrawal recorded before any write settles
    as ``cancelled``; a write already committed, even with a lost reply, settles
    as ``executed``.
    """
    require_name(recording)
    require_name(operation)
    if withdraw_overwrite(station.db, station.journal, station.recorder, recording, operation):
        return PollResult('cancelled')
    return PollResult('executed')
