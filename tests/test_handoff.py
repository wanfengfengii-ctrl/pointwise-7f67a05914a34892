import sqlite3

from survey.capture.candidates import CandidateFrame, RecordingEvent
from survey.device.slots import FrameUnavailable
from survey.recording.requests import RequestConflict
from tests.support import StationCase


class HandoffTests(StationCase):
    def test_old_and_new_tickets(self):
        self.collect()
        old = self.issue_ticket()
        self.assert_page(self.station.take(old.ticket_id, 'night-first', 1), (7,), (('frame-1', 1, 'a'),), (0, 1))
        self.assertEqual(self.station.poll(self.recording, self.replacement(), 'overwrite').edition, 2)
        new = self.issue_ticket('new-issue', 2)
        old_page = self.station.take(old.ticket_id, 'old-rest', 5)
        new_page = self.station.take(new.ticket_id, 'new-page', 5)
        self.assert_page(old_page, (11,), (('frame-2', 2, 'b'),), (1, 2))
        self.assert_page(new_page, (7, 19), (('frame-1', 1, 'a'), ('frame-3', 3, 'b')), (0, 2))
        self.assertEqual((old.measurement.peak, new.measurement.peak), (11, 19))
        self.assertEqual(old.measurement.source.fields(), ('frame-2', 2, 'b'))
        self.assertEqual(new.measurement.source.fields(), ('frame-3', 3, 'b'))
        with self.assertRaises(FrameUnavailable):
            self.station.recorder.read_occurrence('b', 'frame-2')

    def test_historical_version_can_be_issued_after_overwrite(self):
        self.collect()
        self.station.poll(self.recording, self.replacement(), 'overwrite')
        late = self.issue_ticket('late-old-issue', 1)
        self.assert_page(self.station.take(late.ticket_id, 'late-read', 2),
                         (7, 11), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_custody_commit_failure(self):
        self.collect()
        old = self.issue_ticket()
        self.station.take(old.ticket_id, 'night-first', 1)
        event = self.replacement()
        pending = self.station.poll(self.recording, event, 'overwrite', fail_custody_commit=True)
        self.assertEqual((pending.status, pending.edition, pending.reason), ('pending', None, 'custody_commit_failed'))
        self.assertFalse(self.station.custody.has_copy('frame-2'))
        self.assertIsNone(self.station.recorder.operation_outcome('overwrite'))
        self.assertEqual(self.station.recorder.read_occurrence('b', 'frame-2').amplitude, 11)
        self.assertEqual(self.station.current_edition(self.recording).version, 1)
        with self.assertRaises(LookupError):
            self.issue_ticket('new-issue', 2)
        self.assert_page(self.station.take(old.ticket_id, 'old-rest', 5), (11,), (('frame-2', 2, 'b'),), (1, 2))
        self.reopen()
        self.assertEqual(self.station.poll(self.recording, event, 'overwrite').edition, 2)
        self.assertTrue(self.station.custody.has_copy('frame-2'))
        self.assertEqual(self.issue_ticket('new-issue', 2).measurement.peak, 19)

    def test_overwrite_reply_lost(self):
        self.collect()
        old = self.issue_ticket()
        prefix = self.station.take(old.ticket_id, 'night-first', 1)
        event = self.replacement()
        pending = self.station.poll(self.recording, event, 'overwrite', lose_overwrite_reply=True)
        self.assertEqual((pending.status, pending.reason), ('pending', 'overwrite_reply_lost'))
        outcome = self.station.recorder.operation_outcome('overwrite')
        self.assertEqual((outcome.replaced_occurrence, outcome.replacement.occurrence_id), ('frame-2', 'frame-3'))
        self.assertEqual(self.station.recorder.read_occurrence('b', 'frame-3').amplitude, 19)
        self.assertEqual(self.station.current_edition(self.recording).version, 1)
        with self.assertRaises(LookupError):
            self.issue_ticket('too-early', 2)
        self.reopen()
        with self.assertRaises(RequestConflict):
            self.station.poll(self.recording, self.replacement(23), 'overwrite')
        resumed = self.station.poll(self.recording, event, 'overwrite', lose_overwrite_reply=True)
        self.assertEqual((resumed.status, resumed.edition), ('published', 2))
        self.assertEqual(self.station.poll(self.recording, event, 'overwrite'), resumed)
        self.assertEqual(self.station.recorder.operation_outcome('overwrite'), outcome)
        self.assertEqual(self.station.recorder.overwrite_count(), 1)
        self.assertEqual(self.station.take(old.ticket_id, 'night-first', 1), prefix)
        self.assert_page(self.station.take(old.ticket_id, 'old-rest', 3), (11,), (('frame-2', 2, 'b'),), (1, 2))
        new = self.issue_ticket('day', 2)
        self.assert_page(self.station.take(new.ticket_id, 'day-page', 2),
                         (7, 19), (('frame-1', 1, 'a'), ('frame-3', 3, 'b')), (0, 2))

    def test_unsettled_overwrite_keeps_later_poll_out_of_frontier(self):
        self.collect()
        event = self.replacement()
        self.assertEqual(self.station.poll(self.recording, event, 'overwrite', pause_before_overwrite=True).status,
                         'pending')
        self.reopen()
        competing = RecordingEvent((CandidateFrame(0, self.first), CandidateFrame(1, self.second)))
        self.assertEqual(self.station.poll(self.recording, competing, 'competing').reason, 'another_overwrite')
        self.assertEqual(self.station.current_edition(self.recording).version, 1)
        self.assertEqual(self.station.poll(self.recording, event, 'overwrite').edition, 2)
        self.assertEqual(self.station.poll(self.recording, competing, 'competing').edition, 3)
        ticket = self.issue_ticket('read-explicit-history', 3)
        self.assert_page(self.station.take(ticket.ticket_id, 'page-explicit-history', 2),
                         (7, 11), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_page_atomicity(self):
        self.collect()
        ticket = self.issue_ticket()
        with self.station.db.atomic() as conn:
            saved = tuple(conn.execute('''SELECT occurrence_id, sequence, slot, checksum, body
                FROM device_slots WHERE slot = 'b' ''').fetchone())
            conn.execute("DELETE FROM device_slots WHERE slot = 'b'")
        with self.assertRaises(FrameUnavailable):
            self.station.take(ticket.ticket_id, 'whole-page', 2)
        self.assertEqual(self.station.progress.get(ticket.ticket_id), 0)
        self.assertIsNone(self.station.receipts.find_delivery('whole-page', ticket.ticket_id, 2))
        with self.station.db.atomic() as conn:
            conn.execute('INSERT INTO device_slots VALUES (?, ?, ?, ?, ?)', saved)
        self.assert_page(self.station.take(ticket.ticket_id, 'whole-page', 2),
                         (7, 11), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_page_write_failure_rolls_back_receipt_and_position(self):
        self.collect()
        ticket = self.issue_ticket()
        conn = self.station.db.connection
        conn.execute('''CREATE TEMP TRIGGER block_position BEFORE UPDATE ON ticket_progress
            BEGIN SELECT RAISE(ABORT, 'position write blocked'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'position write blocked'):
            self.station.take(ticket.ticket_id, 'whole-page', 2)
        self.assertEqual(self.station.progress.get(ticket.ticket_id), 0)
        self.assertIsNone(self.station.receipts.find_delivery('whole-page', ticket.ticket_id, 2))
        conn.execute('DROP TRIGGER block_position')
        self.assert_page(self.station.take(ticket.ticket_id, 'whole-page', 2),
                         (7, 11), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_page_retry(self):
        self.collect()
        ticket = self.issue_ticket()
        first = self.station.take(ticket.ticket_id, 'page-one', 1)
        self.assert_page(first, (7,), (('frame-1', 1, 'a'),), (0, 1))
        self.assertEqual(self.station.take(ticket.ticket_id, 'page-one', 1), first)
        with self.assertRaises(RequestConflict):
            self.station.take(ticket.ticket_id, 'page-one', 2)
        other = self.issue_ticket('other-ticket')
        with self.assertRaises(RequestConflict):
            self.station.take(other.ticket_id, 'page-one', 1)
        self.assertEqual(self.station.progress.get(other.ticket_id), 0)
        self.assert_page(self.station.take(ticket.ticket_id, 'page-two', 1),
                         (11,), (('frame-2', 2, 'b'),), (1, 2))
        self.assertEqual(self.station.take(ticket.ticket_id, 'page-one', 1), first)
        empty = self.station.take(ticket.ticket_id, 'exhausted', 1)
        self.assert_page(empty, (), (), (2, 2))
