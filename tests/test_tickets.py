from survey.recording.coordinates import FrameWindow
from survey.recording.requests import RequestConflict
from tests.support import StationCase


class TicketTests(StationCase):
    def test_issue_conflicting_retry(self):
        self.collect()
        ticket = self.issue_ticket()
        with self.assertRaises(RequestConflict):
            self.issue_ticket(window=FrameWindow(1, 2))
        self.assertEqual(ticket.receipt, 'issue-old')
        self.reopen()
        self.assertEqual(self.issue_ticket(), ticket)
        self.assertEqual(self.station.progress.get(ticket.ticket_id), 0)
        with self.assertRaises(RequestConflict):
            self.issue_ticket(edition=2)
        self.assertEqual(self.issue_ticket(), ticket)

    def test_invalid_ticket_and_windows_leave_no_success_receipt(self):
        self.collect()
        with self.assertRaises(ValueError):
            self.issue_ticket('ticket-op', window=FrameWindow(0, 3))
        with self.assertRaises(LookupError):
            self.issue_ticket('ticket-op', edition=2)
        ticket = self.issue_ticket('ticket-op')
        with self.assertRaises(LookupError):
            self.station.take(ticket.ticket_id + 1, 'take-op', 2)
        with self.assertRaises(ValueError):
            self.station.take(ticket.ticket_id, 'take-op', 0)
        self.assert_page(self.station.take(ticket.ticket_id, 'take-op', 2),
                         (7, 11), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_ticket_offsets_are_relative_to_its_window(self):
        self.collect()
        ticket = self.issue_ticket('partial-window', window=FrameWindow(1, 2))
        self.assert_page(self.station.take(ticket.ticket_id, 'partial-page', 5),
                         (11,), (('frame-2', 2, 'b'),), (0, 1))
        self.assertEqual(ticket.measurement.count, 1)
        self.assertEqual(ticket.measurement.source.fields(), ('frame-2', 2, 'b'))
