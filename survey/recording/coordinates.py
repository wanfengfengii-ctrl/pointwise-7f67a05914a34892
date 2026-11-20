from dataclasses import dataclass

MAX_COORDINATE = (1 << 63) - 1


def validate_coordinate(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COORDINATE:
        raise ValueError('coordinate must be a nonnegative SQLite integer')
    return value


def require_name(value: str) -> str:
    if type(value) is not str or not value or value.strip() != value or '\x00' in value:
        raise ValueError('identifier must be nonempty text without surrounding whitespace or NUL')
    value.encode('utf-8')
    return value


@dataclass(frozen=True)
class FrameWindow:
    start: int
    stop: int

    def __post_init__(self):
        validate_coordinate(self.start)
        validate_coordinate(self.stop)
        if self.start > self.stop:
            raise ValueError('window start exceeds stop')

    def positions(self) -> range:
        return range(self.start, self.stop)

    @property
    def length(self) -> int:
        return self.stop - self.start


def validate_subwindow(parent: FrameWindow, child: FrameWindow) -> FrameWindow:
    if not isinstance(child, FrameWindow) or not parent.start <= child.start <= child.stop <= parent.stop:
        raise ValueError('window is outside the recording plan')
    return child
