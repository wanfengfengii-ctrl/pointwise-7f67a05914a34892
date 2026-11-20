from hashlib import sha256

from survey.device.slots import DeviceSlotStore, FrameOccurrence, FrameUnavailable
from survey.measurement.amplitude import encode_amplitude


def frame_checksum(amplitude: int) -> str:
    return sha256(encode_amplitude(amplitude)).hexdigest()


def validate_frame_body(frame: FrameOccurrence) -> None:
    if frame.checksum != frame_checksum(frame.amplitude):
        raise ValueError(f'bad frame checksum for {frame.occurrence_id!r}')


def read_occurrence(slots: DeviceSlotStore, slot: str, occurrence_id: str) -> FrameOccurrence:
    frame = slots.load(slot)
    if frame is None or frame.occurrence_id != occurrence_id:
        raise FrameUnavailable(f'{occurrence_id!r} is unavailable in slot {slot!r}')
    validate_frame_body(frame)
    return frame
