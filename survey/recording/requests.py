from sqlite3 import Connection

from survey.recording.plan import RecordingPlan, load_plan


class RequestConflict(ValueError):
    """An existing operation was submitted with different parameters."""


class RecordingRequests:
    def __init__(self, conn: Connection):
        self.conn = conn

    def check_open(self, plan: RecordingPlan) -> bool:
        row = self.conn.execute(
            'SELECT recording FROM recording_opens WHERE recording = ?', (plan.recording,),
        ).fetchone()
        if row is None:
            return False
        if load_plan(self.conn, plan.recording) != plan:
            raise RequestConflict(f'recording {plan.recording!r} has a different plan')
        return True

    def record_open(self, plan: RecordingPlan) -> None:
        self.conn.execute('INSERT INTO recording_opens VALUES (?)', (plan.recording,))

    def find_close(self, operation: str, recording: str) -> str | None:
        row = self.conn.execute(
            'SELECT recording, status FROM recording_closes WHERE operation = ?', (operation,),
        ).fetchone()
        if row is None:
            return None
        if row['recording'] != recording:
            raise RequestConflict(f'close operation {operation!r} has a different recording')
        return row['status']

    def record_close(self, operation: str, recording: str, status: str) -> None:
        self.conn.execute('''INSERT INTO recording_closes VALUES (?, ?, ?)
            ON CONFLICT(operation) DO UPDATE SET status = excluded.status''',
            (operation, recording, status),
        )

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS recording_opens (
            recording TEXT PRIMARY KEY REFERENCES recording_plans(recording)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS recording_closes (
            operation TEXT PRIMARY KEY, recording TEXT NOT NULL REFERENCES recording_plans(recording),
            status TEXT NOT NULL CHECK(status IN ('pending', 'closed'))
        )''')
