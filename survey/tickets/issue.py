from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from survey.measurement.peak import PeakMeasurement, compute_peak
from survey.measurement.provenance import verify_source
from survey.recording.coordinates import FrameWindow, require_name, validate_coordinate, validate_subwindow
from survey.recording.plan import load_plan

if TYPE_CHECKING:
    from survey.recording.station import Station


@dataclass(frozen=True)
class IssuedTicket:
    ticket_id: int
    recording: str
    edition: int
    window: FrameWindow
    receipt: str
    measurement: PeakMeasurement


def _read_issued(station: Station, ticket_id: int, operation: str) -> IssuedTicket:
    header = station.members.load(ticket_id)
    return IssuedTicket(header.ticket_id, header.recording, header.edition, header.window,
                        operation, station.measurements.load(ticket_id))


def issue(station: Station, recording: str, edition: int, window: FrameWindow, operation: str) -> IssuedTicket:
    require_name(operation)
    require_name(recording)
    validate_coordinate(edition)
    if not isinstance(window, FrameWindow):
        raise ValueError('issue requires a frame window')
    with station.db.atomic() as conn:
        ticket_id = station.receipts.find_issue(operation, recording, edition, window)
        if ticket_id is not None:
            return _read_issued(station, ticket_id, operation)
        plan = load_plan(conn, recording)
        validate_subwindow(plan.window, window)
        accepted = station.frontier.load(recording, edition)
        header = station.members.freeze(accepted, window)
        members = station.members.load_page(header.ticket_id, 0, window.length)
        frames = []
        for member in members:
            frame = station.custody.resolve(member.reference)
            verify_source(member.source, frame)
            frames.append(frame)
        measurement = compute_peak(frames)
        station.measurements.save(header.ticket_id, measurement)
        station.progress.start(header.ticket_id)
        station.receipts.record_issue(operation, recording, edition, window, header.ticket_id)
        return IssuedTicket(header.ticket_id, recording, edition, window, operation, measurement)
