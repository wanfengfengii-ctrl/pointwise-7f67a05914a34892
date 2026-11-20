import json
from sqlite3 import Connection

from survey.recording.coordinates import FrameWindow
from survey.recording.requests import RequestConflict


class ReceiptBook:
    def __init__(self, conn: Connection):
        self.conn = conn

    def _find(self, kind: str, operation: str, params: tuple) -> str | None:
        row = self.conn.execute('''SELECT params, result FROM ticket_receipts
            WHERE kind = ? AND operation = ?''', (kind, operation)).fetchone()
        if row is None:
            return None
        if row['params'] != json.dumps(params):
            raise RequestConflict(f'{kind} operation {operation!r} has different parameters')
        return row['result']

    def _record(self, kind: str, operation: str, params: tuple, result: str) -> None:
        self.conn.execute('INSERT INTO ticket_receipts VALUES (?, ?, ?, ?)',
                          (kind, operation, json.dumps(params), result))

    def find_issue(self, operation: str, recording: str, edition: int, window: FrameWindow) -> int | None:
        result = self._find('issue', operation, (recording, edition, window.start, window.stop))
        return None if result is None else int(result)

    def record_issue(self, operation: str, recording: str, edition: int, window: FrameWindow,
                     ticket_id: int) -> None:
        self._record('issue', operation, (recording, edition, window.start, window.stop), str(ticket_id))

    def find_delivery(self, operation: str, ticket_id: int, limit: int) -> str | None:
        return self._find('take', operation, (ticket_id, limit))

    def record_delivery(self, operation: str, ticket_id: int, limit: int, result: str) -> None:
        self._record('take', operation, (ticket_id, limit), result)

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS ticket_receipts (
            kind TEXT NOT NULL CHECK(kind IN ('issue', 'take')), operation TEXT NOT NULL,
            params TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(kind, operation)
        )''')
