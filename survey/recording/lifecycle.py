from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection
from typing import TYPE_CHECKING

from survey.capture.overwrite import heal_cancellations, obligations
from survey.device.slots import FrameUnavailable
from survey.recording.coordinates import FrameWindow, require_name
from survey.recording.plan import RecordingPlan, load_plan, save_plan, validate_plan

if TYPE_CHECKING:
    from survey.recording.station import Station


@dataclass(frozen=True)
class OpenResult:
    plan: RecordingPlan
    stage: str


@dataclass(frozen=True)
class CloseResult:
    recording: str
    status: str


def _stage(conn: Connection, recording: str) -> str:
    row = conn.execute('SELECT stage FROM recording_stages WHERE recording = ?', (recording,)).fetchone()
    if row is None:
        raise LookupError(f'unknown recording {recording!r}')
    return row['stage']


def open(station: Station, recording: str, window: FrameWindow, unit: str) -> OpenResult:
    plan = validate_plan(RecordingPlan(recording, window, unit))
    with station.db.atomic() as conn:
        if station.requests.check_open(plan):
            return OpenResult(plan, _stage(conn, recording))
        if not station.recorder.connected:
            raise FrameUnavailable('recorder is disconnected')
        save_plan(conn, plan)
        conn.execute("INSERT INTO recording_stages VALUES (?, 'active')", (recording,))
        station.requests.record_open(plan)
        return OpenResult(plan, 'active')


def require_active_recording(station: Station, recording: str) -> RecordingPlan:
    conn = station.db.connection
    plan = load_plan(conn, recording)
    if _stage(conn, recording) != 'active' or not station.recorder.connected:
        raise FrameUnavailable(f'recording {recording!r} is closed')
    return plan


def close(station: Station, recording: str, operation: str) -> CloseResult:
    require_name(recording)
    require_name(operation)
    with station.db.atomic() as conn:
        prior = station.requests.find_close(operation, recording)
        if prior == 'closed':
            return CloseResult(recording, 'closed')
        stage = _stage(conn, recording)
        if stage == 'closed':
            station.requests.record_close(operation, recording, 'closed')
            return CloseResult(recording, 'closed')
        heal_cancellations(station.journal, station.recorder)
        if station.journal.pending_overwrites():
            station.requests.record_close(operation, recording, 'pending')
            return CloseResult(recording, 'pending')
        try:
            # Keep every obligation redeemable off the live port: published
            # originals, unsettled candidates and written-but-unpublished frames.
            for reference in obligations(station.journal, station.frontier, station.recorder):
                station.custody.preserve(reference)
        except (FrameUnavailable, ValueError):
            station.requests.record_close(operation, recording, 'pending')
            return CloseResult(recording, 'pending')
        station.recorder.disconnect()
        conn.execute("UPDATE recording_stages SET stage = 'closed' WHERE stage = 'active'")
        station.requests.record_close(operation, recording, 'closed')
        return CloseResult(recording, 'closed')


def install_lifecycle_schema(conn: Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS recording_stages (
        recording TEXT PRIMARY KEY REFERENCES recording_plans(recording),
        stage TEXT NOT NULL CHECK(stage IN ('active', 'closed'))
    )''')
