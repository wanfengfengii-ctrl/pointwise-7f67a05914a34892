from sqlite3 import Connection


class TicketProgress:
    def __init__(self, conn: Connection):
        self.conn = conn

    def start(self, ticket_id: int) -> None:
        self.conn.execute('INSERT INTO ticket_progress VALUES (?, 0)', (ticket_id,))

    def get(self, ticket_id: int) -> int:
        row = self.conn.execute('SELECT position FROM ticket_progress WHERE ticket_id = ?', (ticket_id,)).fetchone()
        if row is None:
            raise LookupError(f'no delivery position for ticket {ticket_id}')
        return row['position']

    def advance(self, ticket_id: int, previous: int, following: int) -> None:
        if following < previous:
            raise ValueError('ticket position cannot move backwards')
        cursor = self.conn.execute('''UPDATE ticket_progress SET position = ?
            WHERE ticket_id = ? AND position = ?''', (following, ticket_id, previous))
        if cursor.rowcount != 1:
            raise RuntimeError('ticket delivery position changed')

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS ticket_progress (
            ticket_id INTEGER PRIMARY KEY REFERENCES tickets(ticket_id),
            position INTEGER NOT NULL CHECK(position >= 0)
        )''')
