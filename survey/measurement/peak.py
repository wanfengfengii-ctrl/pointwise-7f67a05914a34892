from dataclasses import dataclass
from collections.abc import Iterable
from sqlite3 import Connection

from survey.device.slots import FrameOccurrence
from survey.measurement.amplitude import absolute_amplitude, decode_amplitude, encode_amplitude
from survey.measurement.provenance import FrameSource, earlier_source, source_from_occurrence


@dataclass(frozen=True)
class PeakMeasurement:
    count: int
    peak: int | None
    source: FrameSource | None


def compute_peak(frames: Iterable[FrameOccurrence]) -> PeakMeasurement:
    count = 0
    peak = None
    source = None
    for frame in frames:
        count += 1
        magnitude = absolute_amplitude(frame.amplitude)
        candidate = source_from_occurrence(frame)
        if peak is None or magnitude > peak:
            peak = magnitude
            source = candidate
        elif magnitude == peak:
            source = earlier_source(source, candidate)
    return PeakMeasurement(count, peak, source)


class MeasurementStore:
    def __init__(self, conn: Connection):
        self.conn = conn

    def load(self, ticket_id: int) -> PeakMeasurement:
        row = self.conn.execute('SELECT * FROM ticket_measurements WHERE ticket_id = ?', (ticket_id,)).fetchone()
        if row is None:
            raise LookupError(f'no frozen measurement for ticket {ticket_id}')
        if row['peak'] is None:
            return PeakMeasurement(row['frame_count'], None, None)
        source = FrameSource(row['occurrence_id'], row['sequence'], row['slot'])
        return PeakMeasurement(row['frame_count'], decode_amplitude(row['peak']), source)

    def save(self, ticket_id: int, measurement: PeakMeasurement) -> None:
        peak = None if measurement.peak is None else encode_amplitude(measurement.peak)
        source = (None, None, None) if measurement.source is None else measurement.source.fields()
        self.conn.execute('''INSERT INTO ticket_measurements
            (ticket_id, frame_count, peak, occurrence_id, sequence, slot) VALUES (?, ?, ?, ?, ?, ?)''',
            (ticket_id, measurement.count, peak, *source),
        )

    @staticmethod
    def install(conn: Connection) -> None:
        conn.execute('''CREATE TABLE IF NOT EXISTS ticket_measurements (
            ticket_id INTEGER PRIMARY KEY REFERENCES tickets(ticket_id), frame_count INTEGER NOT NULL,
            peak BLOB, occurrence_id TEXT, sequence INTEGER, slot TEXT,
            CHECK((frame_count = 0 AND peak IS NULL AND occurrence_id IS NULL AND sequence IS NULL AND slot IS NULL)
                  OR (frame_count > 0 AND peak IS NOT NULL AND occurrence_id IS NOT NULL
                      AND sequence IS NOT NULL AND slot IS NOT NULL))
        )''')
