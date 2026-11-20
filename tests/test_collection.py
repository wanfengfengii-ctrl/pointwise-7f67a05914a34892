from dataclasses import replace

from survey.capture.candidates import CandidateFrame, OverwriteRequest, RecordingEvent
from survey.device.readout import frame_checksum
from survey.recording.coordinates import FrameWindow
from survey.recording.requests import RequestConflict
from tests.support import StationCase


class CollectionTests(StationCase):
    def test_collect_valid_edition(self):
        result = self.collect()
        self.assertEqual((result.status, result.edition), ('published', 1))
        edition = self.station.current_edition(self.recording)
        self.assertEqual([(member.position, member.reference.occurrence_id, member.reference.sequence,
                           member.reference.slot) for member in edition.members],
                         [(0, 'frame-1', 1, 'a'), (1, 'frame-2', 2, 'b')])
        page = self.station.take(self.issue_ticket().ticket_id, 'collect-take', 2)
        self.assert_page(page, (7, 11), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_open_retry(self):
        result = self.station.open(self.recording, self.window, 'count')
        self.assertEqual(result.stage, 'active')
        self.assertEqual(result.plan.unit, 'count')
        self.reopen()
        self.assertEqual(self.station.open(self.recording, self.window, 'count'), result)
        with self.assertRaises(RequestConflict):
            self.station.open(self.recording, FrameWindow(0, 3), 'count')
        with self.assertRaises(ValueError):
            self.station.open('invalid', FrameWindow(-1, 2))
        with self.assertRaises(ValueError):
            self.station.open('invalid', FrameWindow(0, 2), 'dB')
        self.assertEqual(self.station.open(self.recording, self.window), result)
        self.assertEqual(self.station.open('invalid', FrameWindow(0, 2)).stage, 'active')

    def test_equal_values(self):
        self.collect(7, 7)
        ticket = self.issue_ticket()
        page = self.station.take(ticket.ticket_id, 'distinct-occurrences', 2)
        self.assert_page(page, (7, 7), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_reject_bad_frame(self):
        self.collect()
        bad = self.station.recorder.occupy('c', 99, checksum='0' * 64)
        event = RecordingEvent((CandidateFrame(0, self.first), CandidateFrame(1, bad)))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.station.poll(self.recording, event, 'bad-frame')
        self.assertEqual(self.station.current_edition(self.recording).version, 1)
        with self.assertRaises(LookupError):
            self.issue_ticket('bad-ticket', 2)
        ticket = self.issue_ticket()
        page = self.station.take(ticket.ticket_id, 'still-valid', 2)
        self.assert_page(page, (7, 11), (('frame-1', 1, 'a'), ('frame-2', 2, 'b')), (0, 2))

    def test_occurrence_conflict(self):
        self.collect()
        for fake in (replace(self.second, amplitude=13, checksum=frame_checksum(13)),
                     replace(self.second, slot='a')):
            with self.subTest(fake=fake):
                event = RecordingEvent((CandidateFrame(0, self.first), CandidateFrame(1, fake)))
                with self.assertRaisesRegex(ValueError, 'unverifiable'):
                    self.station.poll(self.recording, event, 'conflicting-occurrence')
        self.assertEqual(self.station.current_edition(self.recording).version, 1)
        self.assertEqual(self.station.recorder.read_occurrence('b', 'frame-2').amplitude, 11)
        self.assertEqual(self.issue_ticket().measurement.peak, 11)

    def test_missing_position(self):
        self.collect()
        event = RecordingEvent((CandidateFrame(0, self.first),))
        with self.assertRaisesRegex(ValueError, 'every recording position'):
            self.station.poll(self.recording, event, 'missing-position')
        with self.assertRaises(LookupError):
            self.issue_ticket('missing-ticket', 2)
        self.assertEqual(self.station.current_edition(self.recording).version, 1)
        self.assertEqual(self.issue_ticket().measurement.count, 2)

    def test_candidate_must_follow_device_order(self):
        self.collect()
        event = RecordingEvent((CandidateFrame(0, self.second), CandidateFrame(1, self.first)))
        with self.assertRaisesRegex(ValueError, 'device order'):
            self.station.poll(self.recording, event, 'reverse-order')
        replacement = OverwriteRequest(0, 'a', 'frame-1', 23, frame_checksum(23))
        with self.assertRaisesRegex(ValueError, 'last in device order'):
            self.station.poll(self.recording, RecordingEvent(event.frames[:1], replacement), 'reverse-replacement')
        self.assertEqual(self.station.recorder.overwrite_count(), 0)
        self.assertEqual(self.station.current_edition(self.recording).version, 1)

    def test_overwrite_can_keep_an_explicit_source_in_its_own_candidate(self):
        self.station.open(self.recording, self.window)
        frame = self.station.recorder.occupy('only-slot', 4)
        change = OverwriteRequest(1, 'only-slot', frame.occurrence_id, -9, frame_checksum(-9))
        event = RecordingEvent((CandidateFrame(0, frame),), change)
        self.assertEqual(self.station.poll(self.recording, event, 'two-occurrences').edition, 1)
        ticket = self.issue_ticket()
        page = self.station.take(ticket.ticket_id, 'same-slot-two-frames', 2)
        self.assert_page(page, (4, -9), (('frame-1', 1, 'only-slot'), ('frame-2', 2, 'only-slot')), (0, 2))
