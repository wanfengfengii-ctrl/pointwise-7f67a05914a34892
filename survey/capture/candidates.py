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


def declared_references(manifest: str) -> tuple[FrameRef, ...]:
    """The explicit original frames a stored declaration adopts."""
    declarations, _ = json.loads(manifest)
    return tuple(FrameRef(*fields) for _, fields in declarations)


def replacement_checksum(manifest: str) -> str | None:
    _, replacement = json.loads(manifest)
    return None if replacement is None else replacement[3]


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

    def declared_references(self) -> tuple[FrameRef, ...]:
        return declared_references(self.manifest)

    def replacement_checksum(self) -> str | None:
        return replacement_checksum(self.manifest)


@dataclass(frozen=True)
class CancelRecord:
    operation: str
    recording: str
    manifest: str
    expected: FrameRef

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

    def load_cancel(self, operation: str) -> CancelRecord | None:
        row = self.conn.execute('SELECT * FROM capture_cancels WHERE operation = ?', (operation,)).fetchone()
        if row is None:
            return None
        return CancelRecord(operation, row['recording'], row['manifest'], FrameRef(*json.loads(row['expected'])))

    def stage(self, operation: str, recording: str, manifest: str,
              expected: FrameRef | None, keeps_expected: bool) -> None:
        expected_data = None if expected is None else json.dumps(expected.fields())
        self.conn.execute('''INSERT INTO capture_poll
            (operation, recording, manifest, expected, keeps_expected, state, edition)
            VALUES (?, ?, ?, ?, ?, 'staged', NULL)''',
            (operation, recording, manifest, expected_data, int(keeps_expected)),
        )

    def record_cancel(self, operation: str, recording: str, manifest: str, expected: FrameRef) -> None:
        """Replace a withdrawn candidate with a persistent cancellation terminal."""
        self.conn.execute('DELETE FROM capture_poll WHERE operation = ?', (operation,))
        self.conn.execute('''INSERT INTO capture_cancels (operation, recording, manifest, expected)
            VALUES (?, ?, ?, ?)''',
            (operation, recording, manifest, json.dumps(expected.fields())),
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

    def pending_overwrites(self, recording: str | None = None,
                           exclude: str | None = None) -> tuple[PollRecord, ...]:
        query = '''SELECT operation FROM capture_poll
            WHERE expected IS NOT NULL AND state IN ('staged', 'prepared')'''
        params: list = []
        if recording is not None:
            query += ' AND recording = ?'
            params.append(recording)
        if exclude is not None:
            query += ' AND operation <> ?'
            params.append(exclude)
        query += ' ORDER BY operation'
        rows = self.conn.execute(query, params)
        return tuple(self.load(row['operation']) for row in rows)

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS capture_poll (
            operation TEXT PRIMARY KEY, recording TEXT NOT NULL REFERENCES recording_plans(recording),
            manifest TEXT NOT NULL, expected TEXT, keeps_expected INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('staged', 'prepared', 'published')),
            edition INTEGER, CHECK((state = 'published') = (edition IS NOT NULL))
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS capture_cancels (
            operation TEXT PRIMARY KEY, recording TEXT NOT NULL REFERENCES recording_plans(recording),
            manifest TEXT NOT NULL, expected TEXT NOT NULL
        )''')
        conn.execute('DROP INDEX IF EXISTS one_unsettled_overwrite')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_unsettled_overwrite_per_recording
            ON capture_poll (recording) WHERE expected IS NOT NULL AND state IN ('staged', 'prepared')''')
