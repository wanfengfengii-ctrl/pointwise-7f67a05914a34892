from dataclasses import dataclass
from sqlite3 import Connection

from survey.device.slots import FrameRef


@dataclass(frozen=True)
class OverwriteOutcome:
    operation: str
    replaced_occurrence: str
    replacement: FrameRef


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

    def record_completed(self, outcome: OverwriteOutcome) -> None:
        self.conn.execute('''INSERT INTO device_overwrites
            (operation, replaced_occurrence, occurrence_id, sequence, slot, checksum)
            VALUES (?, ?, ?, ?, ?, ?)''',
            (outcome.operation, outcome.replaced_occurrence, *outcome.replacement.fields()),
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
