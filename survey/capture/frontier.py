from dataclasses import dataclass
from sqlite3 import Connection

from survey.capture.candidates import CandidateFrame
from survey.device.slots import FrameRef
from survey.recording.coordinates import FrameWindow


@dataclass(frozen=True)
class EditionMember:
    position: int
    reference: FrameRef


@dataclass(frozen=True)
class AcceptedEdition:
    recording: str
    version: int
    members: tuple[EditionMember, ...]


class EditionFrontier:
    def __init__(self, conn: Connection):
        self.conn = conn

    @staticmethod
    def validate_positions(window: FrameWindow, frames: tuple[CandidateFrame, ...],
                           replacement_position: int | None = None) -> None:
        has_replacement = replacement_position is not None
        if has_replacement and (window.length == 0 or replacement_position != window.stop - 1):
            raise ValueError('the new device occurrence must be last in device order')
        if len(frames) + int(has_replacement) != window.length:
            raise ValueError('candidate does not contain every recording position')
        prior_sequence = 0
        for position, item in zip(window.positions(), frames):
            if item.position != position:
                raise ValueError('candidate positions are missing, duplicated or out of order')
            if item.frame.sequence <= prior_sequence:
                raise ValueError('candidate occurrences must be in strictly increasing device order')
            prior_sequence = item.frame.sequence

    def _head(self, recording: str) -> int | None:
        row = self.conn.execute('SELECT version FROM edition_heads WHERE recording = ?', (recording,)).fetchone()
        return None if row is None else row['version']

    def publish(self, recording: str, frames: tuple[CandidateFrame, ...]) -> AcceptedEdition:
        version = (self._head(recording) or 0) + 1
        members = tuple(EditionMember(item.position, item.frame.reference) for item in frames)
        self.conn.execute('INSERT INTO accepted_editions VALUES (?, ?)', (recording, version))
        self.conn.executemany('''INSERT INTO edition_frames
            (recording, version, position, occurrence_id, sequence, slot, checksum)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            ((recording, version, item.position, *item.reference.fields()) for item in members),
        )
        self.conn.execute('''INSERT INTO edition_heads VALUES (?, ?)
            ON CONFLICT(recording) DO UPDATE SET version = excluded.version''', (recording, version))
        return AcceptedEdition(recording, version, members)

    def load(self, recording: str, version: int | None = None) -> AcceptedEdition:
        selected = self._head(recording) if version is None else version
        row = self.conn.execute('SELECT 1 FROM accepted_editions WHERE recording = ? AND version = ?',
                                (recording, selected)).fetchone()
        if row is None:
            raise LookupError(f'edition {selected!r} is not accepted for {recording!r}')
        rows = self.conn.execute('''SELECT * FROM edition_frames
            WHERE recording = ? AND version = ? ORDER BY position''', (recording, selected))
        members = tuple(EditionMember(item['position'], FrameRef.from_row(item)) for item in rows)
        return AcceptedEdition(recording, selected, members)

    def referenced_occurrences(self, slot: str | None = None) -> tuple[FrameRef, ...]:
        query = 'SELECT DISTINCT occurrence_id, sequence, slot, checksum FROM edition_frames'
        params = ()
        if slot is not None:
            query += ' WHERE slot = ?'
            params = (slot,)
        return tuple(FrameRef.from_row(row) for row in self.conn.execute(query + ' ORDER BY sequence', params))

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS accepted_editions (
            recording TEXT NOT NULL REFERENCES recording_plans(recording),
            version INTEGER NOT NULL, PRIMARY KEY(recording, version)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS edition_frames (
            recording TEXT NOT NULL, version INTEGER NOT NULL, position INTEGER NOT NULL,
            occurrence_id TEXT NOT NULL, sequence INTEGER NOT NULL, slot TEXT NOT NULL, checksum TEXT NOT NULL,
            PRIMARY KEY(recording, version, position),
            FOREIGN KEY(recording, version) REFERENCES accepted_editions(recording, version)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS edition_heads (
            recording TEXT PRIMARY KEY, version INTEGER NOT NULL,
            FOREIGN KEY(recording, version) REFERENCES accepted_editions(recording, version)
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS edition_frames_by_slot ON edition_frames(slot)')
