from dataclasses import dataclass
from sqlite3 import Connection, Row

from survey.measurement.amplitude import decode_amplitude, encode_amplitude


class FrameUnavailable(LookupError):
    """The specified original occurrence cannot be read at this source."""


@dataclass(frozen=True)
class FrameRef:
    occurrence_id: str
    sequence: int
    slot: str
    checksum: str

    def fields(self) -> tuple[str, int, str, str]:
        return self.occurrence_id, self.sequence, self.slot, self.checksum

    @classmethod
    def from_row(cls, row: Row):
        return cls(row['occurrence_id'], row['sequence'], row['slot'], row['checksum'])


@dataclass(frozen=True)
class FrameOccurrence:
    occurrence_id: str
    sequence: int
    slot: str
    amplitude: int
    checksum: str

    @property
    def reference(self) -> FrameRef:
        return FrameRef(self.occurrence_id, self.sequence, self.slot, self.checksum)

    def sql_fields(self) -> tuple:
        return (*self.reference.fields(), encode_amplitude(self.amplitude))

    @classmethod
    def from_row(cls, row: Row):
        return cls(row['occurrence_id'], row['sequence'], row['slot'],
                   decode_amplitude(row['body']), row['checksum'])


class DeviceSlotStore:
    def __init__(self, conn: Connection):
        self.conn = conn

    def load(self, slot: str) -> FrameOccurrence | None:
        row = self.conn.execute('''SELECT slot.* FROM device_slots AS slot
            JOIN device_state AS state ON state.singleton = 1 AND state.connected = 1
            WHERE slot.slot = ?''', (slot,)).fetchone()
        return None if row is None else FrameOccurrence.from_row(row)

    def allocate_occurrence(self, slot: str, amplitude: int, checksum: str) -> FrameOccurrence:
        row = self.conn.execute('''UPDATE device_state SET next_sequence = next_sequence + 1
            WHERE singleton = 1 RETURNING next_sequence - 1''').fetchone()
        sequence = row[0]
        return FrameOccurrence(f'frame-{sequence}', sequence, slot, amplitude, checksum)

    def write(self, frame: FrameOccurrence) -> None:
        self.conn.execute('''INSERT INTO device_slots (occurrence_id, sequence, slot, checksum, body)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(slot) DO UPDATE SET
            occurrence_id = excluded.occurrence_id, sequence = excluded.sequence,
            checksum = excluded.checksum, body = excluded.body''', frame.sql_fields())

    def is_connected(self) -> bool:
        row = self.conn.execute('SELECT connected FROM device_state WHERE singleton = 1').fetchone()
        return bool(row[0])

    def set_connected(self, connected: bool) -> None:
        self.conn.execute('UPDATE device_state SET connected = ? WHERE singleton = 1', (int(connected),))

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS device_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            connected INTEGER NOT NULL CHECK(connected IN (0, 1)), next_sequence INTEGER NOT NULL
        )''')
        conn.execute('INSERT OR IGNORE INTO device_state VALUES (1, 1, 1)')
        conn.execute('''CREATE TABLE IF NOT EXISTS device_slots (
            occurrence_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL UNIQUE,
            slot TEXT PRIMARY KEY, checksum TEXT NOT NULL, body BLOB NOT NULL
        )''')
