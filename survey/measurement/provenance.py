from dataclasses import dataclass

from survey.device.slots import FrameOccurrence, FrameRef


@dataclass(frozen=True)
class FrameSource:
    occurrence_id: str
    sequence: int
    slot: str

    def fields(self) -> tuple[str, int, str]:
        return self.occurrence_id, self.sequence, self.slot


def source_from_occurrence(frame: FrameOccurrence | FrameRef) -> FrameSource:
    return FrameSource(frame.occurrence_id, frame.sequence, frame.slot)


def verify_source(expected: FrameSource, frame: FrameOccurrence) -> None:
    if expected != source_from_occurrence(frame):
        raise ValueError(f'original source differs for {expected.occurrence_id!r}')


def earlier_source(first: FrameSource, second: FrameSource) -> FrameSource:
    return first if first.sequence <= second.sequence else second
