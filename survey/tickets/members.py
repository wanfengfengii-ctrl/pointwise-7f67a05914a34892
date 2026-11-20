from dataclasses import dataclass
from sqlite3 import Connection

from survey.capture.frontier import AcceptedEdition
from survey.device.slots import FrameRef
from survey.measurement.provenance import FrameSource, source_from_occurrence
from survey.recording.coordinates import FrameWindow, validate_coordinate


@dataclass(frozen=True)
class TicketHeader:
    ticket_id: int
    recording: str
    edition: int
    window: FrameWindow


@dataclass(frozen=True)
class TicketMember:
    offset: int
    reference: FrameRef

    @property
    def source(self) -> FrameSource:
        return source_from_occurrence(self.reference)


class TicketMembers:
    def __init__(self, conn: Connection):
        self.conn = conn

    def freeze(self, edition: AcceptedEdition, window: FrameWindow) -> TicketHeader:
        cursor = self.conn.execute('''INSERT INTO tickets (recording, edition, start_pos, stop_pos)
            VALUES (?, ?, ?, ?)''', (edition.recording, edition.version, window.start, window.stop))
        ticket_id = cursor.lastrowid
        selected = tuple(item for item in edition.members if window.start <= item.position < window.stop)
        if len(selected) != window.length:
            raise ValueError('edition does not contain the complete ticket window')
        self.conn.executemany('''INSERT INTO ticket_members
            (ticket_id, offset, occurrence_id, sequence, slot, checksum) VALUES (?, ?, ?, ?, ?, ?)''',
            ((ticket_id, offset, *item.reference.fields()) for offset, item in enumerate(selected)),
        )
        return TicketHeader(ticket_id, edition.recording, edition.version, window)

    def load(self, ticket_id: int) -> TicketHeader:
        validate_coordinate(ticket_id)
        row = self.conn.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
        if row is None:
            raise LookupError(f'unknown ticket {ticket_id}')
        return TicketHeader(ticket_id, row['recording'], row['edition'], FrameWindow(row['start_pos'], row['stop_pos']))

    def load_page(self, ticket_id: int, start: int, stop: int) -> tuple[TicketMember, ...]:
        rows = self.conn.execute('''SELECT * FROM ticket_members
            WHERE ticket_id = ? AND offset >= ? AND offset < ? ORDER BY offset''', (ticket_id, start, stop))
        page = tuple(TicketMember(row['offset'], FrameRef.from_row(row)) for row in rows)
        if len(page) != stop - start or any(item.offset != offset for offset, item in enumerate(page, start)):
            raise ValueError(f'fixed members missing from ticket {ticket_id}')
        return page

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY, recording TEXT NOT NULL, edition INTEGER NOT NULL,
            start_pos INTEGER NOT NULL, stop_pos INTEGER NOT NULL,
            FOREIGN KEY(recording, edition) REFERENCES accepted_editions(recording, version)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS ticket_members (
            ticket_id INTEGER NOT NULL REFERENCES tickets(ticket_id), offset INTEGER NOT NULL,
            occurrence_id TEXT NOT NULL, sequence INTEGER NOT NULL, slot TEXT NOT NULL, checksum TEXT NOT NULL,
            PRIMARY KEY(ticket_id, offset)
        )''')
