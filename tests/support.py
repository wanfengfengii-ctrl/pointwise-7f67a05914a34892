from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from survey.capture.candidates import CandidateFrame, OverwriteRequest, RecordingEvent
from survey.device.readout import frame_checksum
from survey.recording.coordinates import FrameWindow
from survey.recording.station import connect_station


class StationCase(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='survey-test-')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'station.sqlite'
        self.station = connect_station(self.path)
        self.addCleanup(self.dispose)
        self.recording = 'pump'
        self.window = FrameWindow(0, 2)

    def dispose(self):
        if self.station is not None:
            self.station.dispose()
            self.station = None

    def reopen(self):
        self.dispose()
        self.station = connect_station(self.path)

    def collect(self, first=7, second=11):
        self.station.open(self.recording, self.window)
        self.first = self.station.recorder.occupy('a', first)
        self.second = self.station.recorder.occupy('b', second)
        event = RecordingEvent((CandidateFrame(0, self.first), CandidateFrame(1, self.second)))
        return self.station.poll(self.recording, event, 'first-poll')

    def replacement(self, amplitude=19):
        request = OverwriteRequest(1, 'b', self.second.occurrence_id, amplitude, frame_checksum(amplitude))
        return RecordingEvent((CandidateFrame(0, self.first),), request)

    def issue_ticket(self, operation='issue-old', edition=1, window=None):
        return self.station.issue(self.recording, edition, self.window if window is None else window, operation)

    def assert_page(self, page, values, sources, positions):
        self.assertEqual(tuple(frame.amplitude for frame in page.frames), values)
        self.assertEqual(tuple(source.fields() for source in page.sources), sources)
        self.assertEqual((page.start, page.next_position), positions)
