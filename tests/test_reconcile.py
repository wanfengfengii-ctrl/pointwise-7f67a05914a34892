from survey.capture.candidates import CandidateFrame, RecordingEvent
from survey.device.slots import FrameUnavailable
from survey.recording.coordinates import FrameWindow
from survey.recording.requests import RequestConflict
from tests.support import StationCase


class ReconcileTests(StationCase):
    def test_close_and_audit(self):
        self.collect()
        old = self.issue_ticket()
        prefix = self.station.take(old.ticket_id, 'night-first', 1)
        self.station.poll(self.recording, self.replacement(), 'overwrite')
        new = self.issue_ticket('new-issue', 2)
        self.assertEqual(self.station.close(self.recording, 'close').status, 'closed')
        self.reopen()
        self.reopen()
        self.assertFalse(self.station.recorder.connected)
        with self.assertRaises(FrameUnavailable):
            self.station.recorder.read_occurrence('a', 'frame-1')
        self.assertEqual(self.station.open(self.recording, self.window).stage, 'closed')
        self.assertEqual(self.issue_ticket(), old)
        self.assertEqual(self.issue_ticket('new-issue', 2), new)
        self.assertEqual(self.station.progress.get(old.ticket_id), 1)
        old_audit = self.station.audit(old.ticket_id)
        new_audit = self.station.audit(new.ticket_id)
        self.assertTrue(old_audit.matches)
        self.assertTrue(new_audit.matches)
        self.assertEqual((old_audit.frozen.count, old_audit.frozen.peak, old_audit.frozen.source.fields()),
                         (2, 11, ('frame-2', 2, 'b')))
        self.assertEqual((new_audit.frozen.count, new_audit.frozen.peak, new_audit.frozen.source.fields()),
                         (2, 19, ('frame-3', 3, 'b')))
        self.assertEqual(tuple(source.fields() for source in old_audit.sources), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')))
        self.assertEqual(tuple(source.fields() for source in new_audit.sources), (('frame-1', 1, 'a'), ('frame-3', 3, 'b')))
        self.assertEqual(self.station.progress.get(old.ticket_id), 1)
        self.assertEqual(self.station.take(old.ticket_id, 'night-first', 1), prefix)
        self.assert_page(self.station.take(old.ticket_id, 'old-rest', 2), (11,), (('frame-2', 2, 'b'),), (1, 2))
        self.assert_page(self.station.take(new.ticket_id, 'new-page', 2),
                         (7, 19), (('frame-1', 1, 'a'), ('frame-3', 3, 'b')), (0, 2))
        self.assertEqual(self.station.close(self.recording, 'close').status, 'closed')
        with self.assertRaises(LookupError):
            self.station.audit(new.ticket_id + 1)

    def test_close_pending(self):
        self.collect()
        self.station.open('other-recording', FrameWindow(0, 0))
        event = self.replacement()
        paused = self.station.poll(self.recording, event, 'overwrite', pause_before_overwrite=True)
        self.assertEqual(paused.reason, 'paused_before_overwrite')
        self.assertEqual(self.station.close(self.recording, 'close').status, 'pending')
        self.reopen()
        self.assertTrue(self.station.recorder.connected)
        self.assertEqual(self.station.recorder.read_occurrence('b', 'frame-2').amplitude, 11)
        with self.assertRaises(RequestConflict):
            self.station.close('other-recording', 'close')
        self.assertEqual(self.station.poll(self.recording, event, 'overwrite').edition, 2)
        self.assertEqual(self.station.close(self.recording, 'close').status, 'closed')
        self.assertEqual(self.station.open('other-recording', FrameWindow(0, 0)).stage, 'closed')

    def test_close_drains_live_sources_for_all_recordings(self):
        self.collect()
        isolated = self.station.recorder.occupy('c', 3)
        self.station.open('other-recording', FrameWindow(4, 5))
        self.station.poll('other-recording', RecordingEvent((CandidateFrame(4, isolated),)), 'other-poll')
        self.station.poll(self.recording, self.replacement(), 'overwrite')
        ticket = self.issue_ticket('day', 2)
        for occurrence in ('frame-1', 'frame-3', 'frame-4'):
            self.assertFalse(self.station.custody.has_copy(occurrence))
        self.assertEqual(self.station.close(self.recording, 'close-all').status, 'closed')
        self.reopen()
        for occurrence in ('frame-1', 'frame-2', 'frame-3', 'frame-4'):
            self.assertTrue(self.station.custody.has_copy(occurrence))
        self.assert_page(self.station.take(ticket.ticket_id, 'after-close', 2),
                         (7, 19), (('frame-1', 1, 'a'), ('frame-4', 4, 'b')), (0, 2))
        other = self.station.issue('other-recording', 1, FrameWindow(4, 5), 'other-late-issue')
        self.assert_page(self.station.take(other.ticket_id, 'other-page', 1), (3,), (('frame-3', 3, 'c'),), (0, 1))

    def test_empty_and_equal_peak(self):
        self.collect(-11, 11)
        for edge in (0, 1, 2):
            with self.subTest(edge=edge):
                ticket = self.issue_ticket(f'empty-{edge}', window=FrameWindow(edge, edge))
                self.assertEqual((ticket.measurement.count, ticket.measurement.peak, ticket.measurement.source), (0, None, None))
                self.assert_page(self.station.take(ticket.ticket_id, f'no-frames-{edge}', 1), (), (), (0, 0))
                empty_audit = self.station.audit(ticket.ticket_id)
                self.assertTrue(empty_audit.matches)
                self.assertEqual(empty_audit.sources, ())
        tied = self.issue_ticket()
        audit = self.station.audit(tied.ticket_id)
        self.assertTrue(audit.matches)
        self.assertEqual((audit.frozen.count, audit.frozen.peak), (2, 11))
        self.assertEqual(audit.frozen.source.fields(), ('frame-1', 1, 'a'))

    def test_large_signed_counts_survive_close_and_receipt_replay(self):
        large = 1 << 130
        self.collect(-large, large - 1)
        ticket = self.issue_ticket()
        self.assertEqual(self.station.close(self.recording, 'close').status, 'closed')
        self.reopen()
        page = self.station.take(ticket.ticket_id, 'big-counts', 2)
        self.assert_page(page, (-large, large - 1), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))
        self.reopen()
        self.assertEqual(self.station.take(ticket.ticket_id, 'big-counts', 2), page)
        audit = self.station.audit(ticket.ticket_id)
        self.assertTrue(audit.matches)
        self.assertEqual(audit.frozen.peak, large)
        self.assertEqual(audit.frozen.source.fields(), ('frame-1', 1, 'a'))

    def test_audit_preserves_frozen_measurement_when_an_original_is_missing(self):
        self.collect()
        ticket = self.issue_ticket()
        with self.station.db.atomic() as conn:
            conn.execute("DELETE FROM device_slots WHERE slot = 'b'")
        audit = self.station.audit(ticket.ticket_id)
        self.assertFalse(audit.matches)
        self.assertIn('offset 1', audit.discrepancies[0])
        self.assertIsNone(audit.observed)
        self.assertEqual(audit.frozen, ticket.measurement)
        self.assertEqual(self.station.progress.get(ticket.ticket_id), 0)
        self.assertEqual(self.station.close(self.recording, 'still-open').status, 'pending')
        self.assertTrue(self.station.recorder.connected)
