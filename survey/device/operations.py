from dataclasses import dataclass
from sqlite3 import Connection

from survey.device.slots import FrameRef


@dataclass(frozen=True)
class OverwriteOutcome:
    operation: str
    replaced_occurrence: str
    replacement: FrameRef


@dataclass(frozen=True)
class CancelledOutcome:
    """The device terminal for an operation that was withdrawn before any write."""

    operation: str
    expected: FrameRef
    checksum: str


class DeviceOperations:
    def __init__(self, conn: Connection):
        self.conn = conn

    def lookup(self, operation: str) -> OverwriteOutcome | None:
        row = self.conn.execute(
            'SELECT * FROM device_overwrites WHERE operation = ?', (operation,),
        ).fetchone()
        if row is None:
            return None
        return OverwriteOutcome(operation, row['replaced_occurrence'], FrameRef.from_row(row))

    def lookup_cancel(self, operation: str) -> CancelledOutcome | None:
        row = self.conn.execute(
            'SELECT * FROM device_cancels WHERE operation = ?', (operation,),
        ).fetchone()
        if row is None:
            return None
        expected = FrameRef(row['replaced_occurrence'], row['replaced_sequence'],
                            row['slot'], row['expected_checksum'])
        return CancelledOutcome(operation, expected, row['replacement_checksum'])

    def record_completed(self, outcome: OverwriteOutcome) -> None:
        self.conn.execute('''INSERT INTO device_overwrites
            (operation, replaced_occurrence, occurrence_id, sequence, slot, checksum)
            VALUES (?, ?, ?, ?, ?, ?)''',
            (outcome.operation, outcome.replaced_occurrence, *outcome.replacement.fields()),
        )

    def operation_for_replacement(self, occurrence_id: str) -> str | None:
        """The executed overwrite whose new occurrence is this identity, if any."""
        row = self.conn.execute(
            'SELECT operation FROM device_overwrites WHERE occurrence_id = ?', (occurrence_id,),
        ).fetchone()
        return None if row is None else row['operation']

    def operation_for_replacement(self, occurrence_id: str) -> str | None:
        """The executed overwrite whose new occurrence is this identity, if any."""
        row = self.conn.execute(
            'SELECT operation FROM device_overwrites WHERE occurrence_id = ?', (occurrence_id,),
        ).fetchone()
        return None if row is None else row['operation']

    def record_cancelled(self, outcome: CancelledOutcome) -> None:
        expected = outcome.expected
        self.conn.execute('''INSERT INTO device_cancels
            (operation, replaced_occurrence, replaced_sequence, slot,
             expected_checksum, replacement_checksum) VALUES (?, ?, ?, ?, ?, ?)''',
            (outcome.operation, expected.occurrence_id, expected.sequence, expected.slot,
             expected.checksum, outcome.checksum),
        )

    def count(self) -> int:
        return self.conn.execute('SELECT count(*) FROM device_overwrites').fetchone()[0]

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS device_overwrites (
            operation TEXT PRIMARY KEY, replaced_occurrence TEXT NOT NULL,
            occurrence_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL UNIQUE,
            slot TEXT NOT NULL, checksum TEXT NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS device_cancels (
            operation TEXT PRIMARY KEY, replaced_occurrence TEXT NOT NULL,
            replaced_sequence INTEGER NOT NULL, slot TEXT NOT NULL,
            expected_checksum TEXT NOT NULL, replacement_checksum TEXT NOT NULL
        )''')
