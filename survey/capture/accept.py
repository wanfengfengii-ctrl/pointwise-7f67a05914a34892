from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from survey.capture.candidates import CandidateFrame, RecordingEvent
from survey.capture.overwrite import has_pending_overwrites, prepare_overwrite, recover_overwrite
from survey.device.recorder import OverwriteReplyLost
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


def poll(station: Station, recording: str, event: RecordingEvent, operation: str, *,
         pause_before_overwrite: bool = False, lose_overwrite_reply: bool = False,
         fail_custody_commit: bool = False) -> PollResult:
    require_name(recording)
    require_name(operation)
    manifest = event.manifest()
    replacement = event.overwrite
    with station.db.atomic():
        record = station.journal.load(operation)
        if record is not None:
            record.check_request(recording, manifest)
            if record.state == 'published':
                return PollResult('published', record.edition)
        plan = require_active_recording(station, recording)
        if record is None:
            if has_pending_overwrites(station.journal):
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
                                   operation, reject_commit=fail_custody_commit)
    except CommitRejected:
        return PollResult('pending', reason='custody_commit_failed')
    if record.state == 'published':
        return PollResult('published', record.edition)
    if pause_before_overwrite:
        return PollResult('pending', reason='paused_before_overwrite')
    try:
        outcome = recover_overwrite(station.recorder, record, replacement, lose_reply=lose_overwrite_reply)
    except OverwriteReplyLost:
        return PollResult('pending', reason='overwrite_reply_lost')

    with station.db.atomic():
        record = station.journal.load(operation)
        if record.state == 'published':
            return PollResult('published', record.edition)
        plan = require_active_recording(station, recording)
        new_frame = station.custody.resolve(outcome.replacement)
        frames = (*event.frames, CandidateFrame(replacement.position, new_frame))
        station.frontier.validate_positions(plan.window, frames)
        _verify_frames(station, frames)
        return _publish(station, recording, operation, frames)
