from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING

from survey.device.slots import FrameOccurrence
from survey.measurement.amplitude import decode_amplitude, encode_amplitude
from survey.measurement.provenance import FrameSource, source_from_occurrence, verify_source
from survey.recording.coordinates import require_name, validate_coordinate

if TYPE_CHECKING:
    from survey.recording.station import Station


@dataclass(frozen=True)
class DeliveryPage:
    ticket_id: int
    start: int
    next_position: int
    frames: tuple[FrameOccurrence, ...]

    @property
    def sources(self) -> tuple[FrameSource, ...]:
        return tuple(source_from_occurrence(frame) for frame in self.frames)

    def encode(self) -> str:
        payload = [(frame.occurrence_id, frame.sequence, frame.slot,
                    encode_amplitude(frame.amplitude).hex(), frame.checksum) for frame in self.frames]
        return json.dumps([self.ticket_id, self.start, self.next_position, payload])

    @classmethod
    def decode(cls, body: str) -> DeliveryPage:
        ticket_id, start, next_position, payload = json.loads(body)
        frames = tuple(FrameOccurrence(occurrence, sequence, slot, decode_amplitude(bytes.fromhex(amplitude)), checksum)
                       for occurrence, sequence, slot, amplitude, checksum in payload)
        return cls(ticket_id, start, next_position, frames)


def take(station: Station, ticket_id: int, operation: str, limit: int) -> DeliveryPage:
    require_name(operation)
    validate_coordinate(ticket_id)
    if validate_coordinate(limit) == 0:
        raise ValueError('page size must be positive')
    with station.db.atomic():
        prior = station.receipts.find_delivery(operation, ticket_id, limit)
        if prior is not None:
            return DeliveryPage.decode(prior)
        ticket = station.members.load(ticket_id)
        start = station.progress.get(ticket_id)
        stop = min(start + limit, ticket.window.length)
        members = station.members.load_page(ticket_id, start, stop)
        frames = []
        for member in members:
            frame = station.custody.resolve(member.reference)
            verify_source(member.source, frame)
            frames.append(frame)
        page = DeliveryPage(ticket_id, start, stop, tuple(frames))
        station.receipts.record_delivery(operation, ticket_id, limit, page.encode())
        station.progress.advance(ticket_id, start, stop)
        return page
