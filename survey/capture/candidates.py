from dataclasses import dataclass
import json
from sqlite3 import Connection

from survey.device.readout import frame_checksum, validate_frame_body
from survey.device.slots import FrameOccurrence, FrameRef
from survey.recording.coordinates import require_name, validate_coordinate
from survey.recording.requests import RequestConflict


@dataclass(frozen=True)
class CandidateFrame:
    position: int
    frame: FrameOccurrence


@dataclass(frozen=True)
class OverwriteRequest:
    position: int
    slot: str
    expected_occurrence: str
    amplitude: int
    checksum: str


@dataclass(frozen=True)
class RecordingEvent:
    frames: tuple[CandidateFrame, ...]
    overwrite: OverwriteRequest | None = None

    def manifest(self) -> str:
        declarations = []
        for item in self.frames:
            validate_coordinate(item.position)
            require_name(item.frame.occurrence_id)
            require_name(item.frame.slot)
            validate_coordinate(item.frame.sequence)
            validate_frame_body(item.frame)
            declarations.append((item.position, item.frame.reference.fields()))
        replacement = None
        if self.overwrite is not None:
            request = self.overwrite
            validate_coordinate(request.position)
            require_name(request.slot)
            require_name(request.expected_occurrence)
            if request.checksum != frame_checksum(request.amplitude):
                raise ValueError('bad replacement checksum')
            replacement = (request.position, request.slot, request.expected_occurrence, request.checksum)
        return json.dumps([declarations, replacement], separators=(',', ':'))


@dataclass(frozen=True)
class PollRecord:
    operation: str
    recording: str
    manifest: str
    expected: FrameRef | None
    keeps_expected: bool
    state: str
    edition: int | None

    def check_request(self, recording: str, manifest: str) -> None:
        if (self.recording, self.manifest) != (recording, manifest):
            raise RequestConflict(f'poll operation {self.operation!r} has different parameters')


class PollJournal:
    def __init__(self, conn: Connection):
        self.conn = conn

    def load(self, operation: str) -> PollRecord | None:
        row = self.conn.execute('SELECT * FROM capture_poll WHERE operation = ?', (operation,)).fetchone()
        if row is None:
            return None
        expected = None if row['expected'] is None else FrameRef(*json.loads(row['expected']))
        return PollRecord(operation, row['recording'], row['manifest'], expected,
                          bool(row['keeps_expected']), row['state'], row['edition'])

    def stage(self, operation: str, recording: str, manifest: str,
              expected: FrameRef | None, keeps_expected: bool) -> None:
        expected_data = None if expected is None else json.dumps(expected.fields())
        self.conn.execute('''INSERT INTO capture_poll
            (operation, recording, manifest, expected, keeps_expected, state, edition)
            VALUES (?, ?, ?, ?, ?, 'staged', NULL)''',
            (operation, recording, manifest, expected_data, int(keeps_expected)),
        )

    def mark_prepared(self, operation: str) -> None:
        cursor = self.conn.execute('''UPDATE capture_poll SET state = 'prepared'
            WHERE operation = ? AND state = 'staged' AND expected IS NOT NULL''', (operation,))
        if cursor.rowcount != 1:
            raise RuntimeError('overwrite has no staged candidate')

    def mark_published(self, operation: str, edition: int) -> None:
        cursor = self.conn.execute('''UPDATE capture_poll SET state = 'published', edition = ?
            WHERE operation = ? AND (state = 'prepared' OR (state = 'staged' AND expected IS NULL))''',
            (edition, operation),
        )
        if cursor.rowcount != 1:
            raise RuntimeError('poll has no publishable candidate')

    def pending_overwrites(self) -> tuple[str, ...]:
        rows = self.conn.execute('''SELECT operation FROM capture_poll
            WHERE expected IS NOT NULL AND state <> 'published' ORDER BY operation''')
        return tuple(row['operation'] for row in rows)

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS capture_poll (
            operation TEXT PRIMARY KEY, recording TEXT NOT NULL REFERENCES recording_plans(recording),
            manifest TEXT NOT NULL, expected TEXT, keeps_expected INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('staged', 'prepared', 'published')),
            edition INTEGER, CHECK((state = 'published') = (edition IS NOT NULL))
        )''')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_unsettled_overwrite
            ON capture_poll ((1)) WHERE expected IS NOT NULL AND state <> 'published' ''')
