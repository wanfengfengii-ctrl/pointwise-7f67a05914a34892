from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from survey.device.slots import FrameUnavailable
from survey.measurement.peak import PeakMeasurement, compute_peak
from survey.measurement.provenance import FrameSource, verify_source

if TYPE_CHECKING:
    from survey.recording.station import Station


@dataclass(frozen=True)
class AuditResult:
    ticket_id: int
    frozen: PeakMeasurement
    sources: tuple[FrameSource, ...]
    observed: PeakMeasurement | None
    discrepancies: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.discrepancies


def compare_frozen_measurement(frozen: PeakMeasurement, observed: PeakMeasurement) -> tuple[str, ...]:
    return tuple(field for field in ('count', 'peak', 'source')
                 if getattr(frozen, field) != getattr(observed, field))


def audit(station: Station, ticket_id: int) -> AuditResult:
    with station.db.atomic():
        ticket = station.members.load(ticket_id)
        frozen = station.measurements.load(ticket_id)
        try:
            members = station.members.load_page(ticket_id, 0, ticket.window.length)
        except ValueError as error:
            return AuditResult(ticket_id, frozen, (), None, (str(error),))
        sources = tuple(member.source for member in members)
        frames = []
        failures = []
        for member in members:
            try:
                frame = station.custody.resolve(member.reference)
                verify_source(member.source, frame)
                frames.append(frame)
            except (FrameUnavailable, ValueError) as error:
                failures.append(f'offset {member.offset}: {error}')
        if failures:
            return AuditResult(ticket_id, frozen, sources, None, tuple(failures))
        observed = compute_peak(frames)
        differences = compare_frozen_measurement(frozen, observed)
        return AuditResult(ticket_id, frozen, sources, observed, differences)
