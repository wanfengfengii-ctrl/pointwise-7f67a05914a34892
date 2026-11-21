from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from survey.capture.candidates import CandidateFrame, OverwriteRequest, RecordingEvent
from survey.device.readout import frame_checksum
from survey.recording.coordinates import FrameWindow
from survey.recording.requests import RequestConflict
from survey.recording.station import connect_station


class CancelCase(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='survey-cancel-')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'station.sqlite'
        self.station = connect_station(self.path)
        self.addCleanup(self.dispose)
        self.window = FrameWindow(0, 2)

    def dispose(self):
        if self.station is not None:
            self.station.dispose()
            self.station = None

    def reopen(self):
        self.dispose()
        self.station = connect_station(self.path)

    def seed(self):
        station = self.station
        station.open('alpha', self.window)
        self.p = station.recorder.occupy('p', 5)
        self.a = station.recorder.occupy('a', 7)
        self.b = station.recorder.occupy('b', 11)
        initial = station.poll('alpha', self.first_event(), 'alpha-first')
        self.old = station.issue('alpha', initial.edition, self.window, 'old-ticket')
        station.take(self.old.ticket_id, 'old-prefix', 1)
        return initial

    def first_event(self):
        return RecordingEvent((CandidateFrame(0, self.p), CandidateFrame(1, self.b)))

    def swap(self, old, amplitude, *prefix):
        request = OverwriteRequest(len(prefix), old.slot, old.occurrence_id, amplitude,
                                   frame_checksum(amplitude))
        return RecordingEvent(tuple(CandidateFrame(pos, frame) for pos, frame in enumerate(prefix)),
                              request)

    def alpha_event(self):
        return self.swap(self.b, 19, self.a)

    def command(self, operation, event):
        record = self.station.journal.load(operation)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, 'prepared')
        return (lambda op, expected, amplitude, checksum:
                self.station.recorder.overwrite(op, expected, amplitude, checksum),
                operation, record.expected, event.overwrite.amplitude, event.overwrite.checksum)


class CancelTests(CancelCase):
    def test_withdraw_before_device_write_settles_cancelled_and_late_command_is_refused(self):
        self.seed()
        event = self.alpha_event()
        paused = self.station.poll('alpha', event, 'alpha-swap', pause_before_overwrite=True)
        self.assertEqual((paused.status, paused.reason), ('pending', 'paused_before_overwrite'))
        prepared = self.command('alpha-swap', event)
        result = self.station.cancel_overwrite('alpha', 'alpha-swap')
        self.assertEqual((result.status, result.edition), ('cancelled', None))
        self.assertEqual(self.station.poll('alpha', event, 'alpha-swap').status, 'cancelled')
        # The saved device command arrives late: it must not change the slot.
        send, operation, expected, amplitude, checksum = prepared
        with self.assertRaises(Exception):
            send(operation, expected, amplitude, checksum)
        self.assertEqual(self.station.recorder.read_occurrence('b', self.b.occurrence_id).amplitude, 11)
        self.assertEqual(self.station.recorder.overwrite_count(), 0)
        self.assertEqual(self.station.current_edition('alpha').version, 1)
        with self.assertRaises(LookupError):
            self.station.issue('alpha', 2, self.window, 'too-early')
        self.reopen()
        self.assertEqual(self.station.poll('alpha', event, 'alpha-swap').status, 'cancelled')
        self.assertEqual(self.station.recorder.overwrite_count(), 0)
        page = self.station.take(self.old.ticket_id, 'old-tail', 2)
        self.assertEqual(tuple(frame.amplitude for frame in page.frames), (11,))

    def test_late_command_after_withdrawal_does_not_write(self):
        self.seed()
        event = self.alpha_event()
        self.station.poll('alpha', event, 'alpha-swap', pause_before_overwrite=True)
        command = self.command('alpha-swap', event)
        self.assertEqual(self.station.cancel_overwrite('alpha', 'alpha-swap').status, 'cancelled')
        # The command was formed before withdrawal; replaying it now must not write.
        import survey.device.recorder as recorder_module
        with self.assertRaises(recorder_module.OverwriteWithdrawn):
            command[0](*command[1:])
        self.assertEqual(self.station.recorder.read_occurrence('b', self.b.occurrence_id).amplitude, 11)
        self.assertEqual(self.station.recorder.overwrite_count(), 0)

    def test_cancelled_replay_requires_original_declaration_and_has_no_edition(self):
        self.seed()
        event = self.alpha_event()
        self.station.poll('alpha', event, 'alpha-swap', pause_before_overwrite=True)
        cancelled = self.station.cancel_overwrite('alpha', 'alpha-swap')
        self.assertEqual((cancelled.status, cancelled.edition), ('cancelled', None))
        # Same number, different amplitude: rejected, not silently accepted.
        with self.assertRaises(RequestConflict):
            self.station.poll('alpha', self.swap(self.b, 23, self.a), 'alpha-swap')
        # Same number and declaration is a replay of the terminal.
        replay = self.station.poll('alpha', event, 'alpha-swap')
        self.assertEqual((replay.status, replay.edition), ('cancelled', None))
        # A plain (non-overwrite) declaration under the same number also conflicts.
        with self.assertRaises(RequestConflict):
            self.station.poll('alpha', RecordingEvent((CandidateFrame(0, self.p),
                                                       CandidateFrame(1, self.b))), 'alpha-swap')

    def test_withdraw_after_device_write_returns_executed_and_poll_resumes_edition_two(self):
        self.seed()
        event = self.alpha_event()
        self.station.poll('alpha', event, 'alpha-swap', lose_overwrite_reply=True)
        outcome = self.station.recorder.operation_outcome('alpha-swap')
        self.assertIsNotNone(outcome)
        result = self.station.cancel_overwrite('alpha', 'alpha-swap')
        self.assertEqual((result.status, result.edition), ('executed', None))
        self.reopen()
        self.assertEqual(self.station.cancel_overwrite('alpha', 'alpha-swap').status, 'executed')
        resumed = self.station.poll('alpha', event, 'alpha-swap')
        self.assertEqual((resumed.status, resumed.edition), ('published', 2))
        self.assertEqual(self.station.poll('alpha', event, 'alpha-swap'), resumed)
        self.assertEqual(self.station.recorder.overwrite_count(), 1)
        self.assertEqual(self.station.recorder.operation_outcome('alpha-swap'), outcome)
        new = self.station.issue('alpha', 2, self.window, 'new-ticket')
        page = self.station.take(new.ticket_id, 'new-page', 2)
        self.assertEqual(tuple(frame.amplitude for frame in page.frames), (7, 19))

    def test_cancel_unknown_wrong_recording_and_plain_collection(self):
        self.seed()
        with self.assertRaises(LookupError):
            self.station.cancel_overwrite('alpha', 'missing')
        event = self.alpha_event()
        self.station.poll('alpha', event, 'alpha-swap', pause_before_overwrite=True)
        self.station.open('beta', FrameWindow(0, 1))
        with self.assertRaises(RequestConflict):
            self.station.cancel_overwrite('beta', 'alpha-swap')
        with self.assertRaises(ValueError):
            self.station.cancel_overwrite('alpha', 'alpha-first')

    def test_cancel_survives_station_settlement_failure_without_new_edition(self):
        self.seed()
        event = self.alpha_event()
        self.station.poll('alpha', event, 'alpha-swap', lose_overwrite_reply=True)
        # Reject the station settlement transaction that would publish the response.
        with self.station.db.atomic() as conn:
            conn.execute('''CREATE TEMP TRIGGER block_edition BEFORE INSERT ON accepted_editions
                BEGIN SELECT RAISE(ABORT, 'edition write blocked'); END''')
        with self.assertRaises(Exception):
            self.station.poll('alpha', event, 'alpha-swap')
        conn = self.station.db.connection
        conn.execute('DROP TRIGGER block_edition')
        self.assertEqual(self.station.current_edition('alpha').version, 1)
        with self.assertRaises(LookupError):
            self.station.issue('alpha', 2, self.window, 'blocked')
        self.reopen()
        resumed = self.station.poll('alpha', event, 'alpha-swap')
        self.assertEqual((resumed.status, resumed.edition), ('published', 2))

    def test_withdrawal_settlement_failure_is_reclaimed_from_device_terminal(self):
        self.seed()
        event = self.alpha_event()
        self.station.poll('alpha', event, 'alpha-swap', pause_before_overwrite=True)
        # The device notices the withdrawal, but the station journal commit fails.
        with self.station.db.atomic() as conn:
            conn.execute('''CREATE TEMP TRIGGER block_cancel BEFORE INSERT ON capture_cancels
                BEGIN SELECT RAISE(ABORT, 'cancel write blocked'); END''')
        with self.assertRaises(Exception):
            self.station.cancel_overwrite('alpha', 'alpha-swap')
        conn = self.station.db.connection
        conn.execute('DROP TRIGGER block_cancel')
        # The device terminal is already durable: no new edition, no write.
        self.assertIsNotNone(self.station.recorder.cancellation_outcome('alpha-swap'))
        self.assertIsNone(self.station.recorder.operation_outcome('alpha-swap'))
        self.assertEqual(self.station.current_edition('alpha').version, 1)
        self.reopen()
        self.assertEqual(self.station.poll('alpha', event, 'alpha-swap').status, 'cancelled')
        self.assertEqual(self.station.recorder.overwrite_count(), 0)
        self.reopen()
        self.assertEqual(self.station.cancel_overwrite('alpha', 'alpha-swap').status, 'cancelled')
        page = self.station.take(self.old.ticket_id, 'old-tail', 2)
        self.assertEqual(tuple(frame.amplitude for frame in page.frames), (11,))
        self.assertEqual(self.station.close('alpha', 'end-shift').status, 'closed')


class InterleaveTests(CancelCase):
    def test_unrelated_recording_publishes_while_overwrite_paused(self):
        self.seed()
        station = self.station
        c = station.recorder.occupy('c', 29)
        e = station.recorder.occupy('e', 3)
        station.open('beta', FrameWindow(0, 1))
        station.open('gamma', FrameWindow(0, 1))
        alpha_swap = self.alpha_event()
        station.poll('alpha', alpha_swap, 'alpha-swap', pause_before_overwrite=True)
        self.assertEqual(station.poll('beta', RecordingEvent((CandidateFrame(0, c),)), 'beta-read').status,
                         'published')
        # A later candidate in the same recording still waits for the settlement.
        late = station.poll('alpha', self.first_event(), 'alpha-after')
        self.assertEqual((late.status, late.reason), ('pending', 'another_overwrite'))
        self.assertEqual(station.poll('gamma', RecordingEvent((CandidateFrame(0, e),)), 'gamma-read').status,
                         'published')
        self.assertEqual(station.poll('alpha', alpha_swap, 'alpha-swap').edition, 2)

    def test_cross_recording_overwrite_preserves_pending_explicit_original(self):
        self.seed()
        station = self.station
        station.open('beta', FrameWindow(0, 1))
        alpha_swap = self.alpha_event()
        station.poll('alpha', alpha_swap, 'alpha-swap', pause_before_overwrite=True)
        # beta wants to replace the physical slot alpha's explicit prefix frame lives in.
        beta_event = self.swap(self.a, 17)
        self.assertFalse(station.custody.has_copy(self.a.occurrence_id))
        beta = station.poll('beta', beta_event, 'beta-swap')
        self.assertEqual((beta.status, beta.edition), ('published', 1))
        # The explicit original was persisted before the overwrite took the slot.
        self.assertTrue(station.custody.has_copy(self.a.occurrence_id))
        self.assertEqual(station.custody.resolve(self.a.reference).amplitude, 7)
        self.assertEqual(station.poll('alpha', alpha_swap, 'alpha-swap').edition, 2)
        alpha_new = station.issue('alpha', 2, self.window, 'alpha-new')
        alpha_page = station.take(alpha_new.ticket_id, 'alpha-page', 2)
        self.assertEqual(tuple(frame.amplitude for frame in alpha_page.frames), (7, 19))
        beta_page = station.take(station.issue('beta', 1, FrameWindow(0, 1), 'beta-ticket').ticket_id,
                                 'beta-page', 1)
        self.assertEqual(tuple(frame.amplitude for frame in beta_page.frames), (17,))

    def test_written_but_unpublished_new_frame_survives_cross_recording_overwrite(self):
        self.seed()
        station = self.station
        station.open('beta', FrameWindow(0, 1))
        alpha_swap = self.alpha_event()
        station.poll('alpha', alpha_swap, 'alpha-swap', lose_overwrite_reply=True)
        written = station.recorder.operation_outcome('alpha-swap')
        self.assertIsNotNone(written)
        # beta now overwrites the occurrence the device just wrote, before alpha settles.
        beta_event = self.swap(written.replacement, 23)
        beta = station.poll('beta', beta_event, 'beta-swap')
        self.assertEqual((beta.status, beta.edition), ('published', 1))
        self.assertTrue(station.custody.has_copy(written.replacement.occurrence_id))
        self.reopen()
        station = self.station
        resumed = station.poll('alpha', alpha_swap, 'alpha-swap')
        self.assertEqual((resumed.status, resumed.edition), ('published', 2))
        new = station.issue('alpha', 2, self.window, 'alpha-new')
        page = station.take(new.ticket_id, 'alpha-page', 2)
        self.assertEqual(tuple(frame.amplitude for frame in page.frames), (7, 19))
        self.assertEqual(page.sources[1].occurrence_id, written.replacement.occurrence_id)
        # The displaced written frame is still redeemable at its original identity.
        self.assertEqual(station.custody.resolve(written.replacement).amplitude, 19)

    def test_candidate_added_after_preparation_survives_already_cleared_overwrite(self):
        self.seed()
        station = self.station
        station.open('beta', FrameWindow(0, 1))
        alpha_swap = self.alpha_event()
        station.poll('alpha', alpha_swap, 'alpha-swap', pause_before_overwrite=True)
        # After alpha is prepared, beta adopts the live occupant of b as its
        # explicit original and publishes an edition that references it.
        live_b = station.recorder.read_occurrence('b', self.b.occurrence_id)
        beta = station.poll('beta', RecordingEvent((CandidateFrame(0, live_b),)), 'beta-direct')
        self.assertEqual(beta.status, 'published')
        # Alpha's already-cleared overwrite must keep beta's original redeemable.
        self.assertEqual(station.poll('alpha', alpha_swap, 'alpha-swap').edition, 2)
        self.assertTrue(station.custody.has_copy(self.b.occurrence_id))
        self.assertEqual(station.custody.resolve(self.b.reference).amplitude, 11)
        beta_page = station.take(station.issue('beta', 1, FrameWindow(0, 1), 'beta-ticket').ticket_id,
                                 'beta-page', 1)
        self.assertEqual(tuple(frame.amplitude for frame in beta_page.frames), (11,))

    def test_conflicting_slot_waits_while_unexecuted_then_proceeds_after_withdrawal(self):
        self.seed()
        station = self.station
        station.open('beta', FrameWindow(0, 1))
        station.poll('alpha', self.alpha_event(), 'alpha-swap', pause_before_overwrite=True)
        beta_event = self.swap(self.b, 23)
        waiting = station.poll('beta', beta_event, 'beta-swap')
        self.assertEqual((waiting.status, waiting.reason), ('pending', 'another_overwrite'))
        station.cancel_overwrite('alpha', 'alpha-swap')
        accepted = station.poll('beta', beta_event, 'beta-swap')
        self.assertEqual((accepted.status, accepted.edition), ('published', 1))

    def test_close_waits_for_open_obligation_then_settles_and_reopens_readonly(self):
        self.seed()
        self.station.poll('alpha', self.alpha_event(), 'alpha-swap', pause_before_overwrite=True)
        self.assertEqual(self.station.close('alpha', 'end-shift').status, 'pending')
        self.station.cancel_overwrite('alpha', 'alpha-swap')
        self.assertEqual(self.station.close('alpha', 'end-shift').status, 'closed')
        self.reopen()
        self.assertFalse(self.station.recorder.connected)
        self.assertEqual(self.station.take(self.old.ticket_id, 'old-rest', 2).frames[0].amplitude, 11)


if __name__ == '__main__':
    unittest.main()
