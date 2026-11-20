from sqlite3 import Connection

from survey.device.readout import validate_frame_body
from survey.device.recorder import Recorder
from survey.device.slots import FrameOccurrence, FrameRef, FrameUnavailable


class CustodyStore:
    def __init__(self, conn: Connection, recorder: Recorder):
        self.conn = conn
        self.recorder = recorder

    def resolve(self, reference: FrameRef) -> FrameOccurrence:
        row = self.conn.execute(
            'SELECT * FROM custody_frames WHERE occurrence_id = ?', (reference.occurrence_id,),
        ).fetchone()
        if row is None:
            frame = self.recorder.read_occurrence(reference.slot, reference.occurrence_id)
        else:
            frame = FrameOccurrence.from_row(row)
            validate_frame_body(frame)
        if frame.reference != reference:
            raise FrameUnavailable(f'facts differ for occurrence {reference.occurrence_id!r}')
        return frame

    def preserve(self, reference: FrameRef) -> None:
        frame = self.resolve(reference)
        if not self.has_copy(reference.occurrence_id):
            self.conn.execute('''INSERT INTO custody_frames
                (occurrence_id, sequence, slot, checksum, body) VALUES (?, ?, ?, ?, ?)''', frame.sql_fields())

    def has_copy(self, occurrence_id: str) -> bool:
        row = self.conn.execute('SELECT 1 FROM custody_frames WHERE occurrence_id = ?', (occurrence_id,))
        return row.fetchone() is not None

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS custody_frames (
            occurrence_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
            slot TEXT NOT NULL, checksum TEXT NOT NULL, body BLOB NOT NULL
        )''')
