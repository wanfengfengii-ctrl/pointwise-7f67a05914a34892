from dataclasses import dataclass
from sqlite3 import Connection

from survey.measurement.amplitude import RAW_COUNT_UNIT
from survey.recording.coordinates import FrameWindow, require_name


@dataclass(frozen=True)
class RecordingPlan:
    recording: str
    window: FrameWindow
    unit: str = RAW_COUNT_UNIT


def validate_plan(plan: RecordingPlan) -> RecordingPlan:
    require_name(plan.recording)
    if not isinstance(plan.window, FrameWindow):
        raise ValueError('recording requires a frame window')
    if plan.unit != RAW_COUNT_UNIT:
        raise ValueError('recording uses raw integer counts')
    return plan


def load_plan(conn: Connection, recording: str) -> RecordingPlan:
    row = conn.execute(
        'SELECT start_pos, stop_pos, unit FROM recording_plans WHERE recording = ?',
        (recording,),
    ).fetchone()
    if row is None:
        raise LookupError(f'unknown recording {recording!r}')
    return RecordingPlan(recording, FrameWindow(row['start_pos'], row['stop_pos']), row['unit'])


def save_plan(conn: Connection, plan: RecordingPlan) -> None:
    validate_plan(plan)
    conn.execute(
        'INSERT INTO recording_plans (recording, start_pos, stop_pos, unit) VALUES (?, ?, ?, ?)',
        (plan.recording, plan.window.start, plan.window.stop, plan.unit),
    )


def install_plan_schema(conn: Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS recording_plans (
        recording TEXT PRIMARY KEY, start_pos INTEGER NOT NULL, stop_pos INTEGER NOT NULL,
        unit TEXT NOT NULL, CHECK(start_pos >= 0 AND stop_pos >= start_pos)
    )''')
