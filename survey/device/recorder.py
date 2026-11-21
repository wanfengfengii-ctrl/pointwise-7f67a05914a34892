from sqlite3 import Connection

from survey.device.operations import CancelledOutcome, DeviceOperations, OverwriteOutcome
from survey.device.readout import frame_checksum, read_occurrence
from survey.device.slots import DeviceSlotStore, FrameOccurrence, FrameRef, FrameUnavailable
from survey.recording.coordinates import require_name
from survey.recording.requests import RequestConflict
from survey.recording.transactions import StationDatabase


class OverwriteReplyLost(RuntimeError):
    """The device committed the overwrite before its reply was lost."""


class OverwriteWithdrawn(RuntimeError):
    """A late overwrite command arrived after the operation was withdrawn."""


class Recorder:
    def __init__(self, db: StationDatabase):
        self.db = db
        self.slots = DeviceSlotStore(db.connection)
        self.operations = DeviceOperations(db.connection)

    @property
    def connected(self) -> bool:
        return self.slots.is_connected()

    def occupy(self, slot: str, amplitude: int, *, checksum: str | None = None) -> FrameOccurrence:
        require_name(slot)
        computed = frame_checksum(amplitude)
        checksum = computed if checksum is None else require_name(checksum)
        with self.db.atomic():
            if not self.connected:
                raise FrameUnavailable('recorder is disconnected')
            if self.slots.load(slot) is not None:
                raise ValueError(f'slot {slot!r} already occupied; use the station overwrite flow')
            frame = self.slots.allocate_occurrence(slot, amplitude, checksum)
            self.slots.write(frame)
        return frame

    def current_reference(self, slot: str, occurrence_id: str) -> FrameRef:
        frame = self.slots.load(slot)
        if frame is None or frame.occurrence_id != occurrence_id:
            raise FrameUnavailable(f'expected occurrence {occurrence_id!r} is absent from {slot!r}')
        return frame.reference

    def read_occurrence(self, slot: str, occurrence_id: str) -> FrameOccurrence:
        return read_occurrence(self.slots, slot, occurrence_id)

    def overwrite(self, operation: str, expected: FrameRef, amplitude: int,
                  checksum: str, *, lose_reply: bool = False) -> OverwriteOutcome:
        require_name(operation)
        if checksum != frame_checksum(amplitude):
            raise ValueError('bad replacement checksum')
        with self.db.atomic():
            withdrawn = self.operations.lookup_cancel(operation)
            if withdrawn is not None:
                self.check_cancel(withdrawn, expected, checksum)
                raise OverwriteWithdrawn(f'overwrite {operation!r} was withdrawn before it reached the device')
            prior = self.operation_outcome(operation)
            if prior is not None:
                self.check_outcome(prior, expected, checksum)
                return prior
            if self.current_reference(expected.slot, expected.occurrence_id) != expected:
                raise FrameUnavailable('slot facts changed before overwrite')
            frame = self.slots.allocate_occurrence(expected.slot, amplitude, checksum)
            self.slots.write(frame)
            outcome = OverwriteOutcome(operation, expected.occurrence_id, frame.reference)
            self.operations.record_completed(outcome)
        if lose_reply:
            raise OverwriteReplyLost(operation)
        return outcome

    def withdraw(self, operation: str, expected: FrameRef, checksum: str) -> CancelledOutcome | None:
        """Record the device terminal for a withdrawal that preceded any write.

        Commits on its own so the terminal outlives a later station settlement
        failure.  Returns ``None`` when the device already executed this
        operation, so the caller settles it as a completed overwrite instead.
        """
        require_name(operation)
        with self.db.atomic():
            prior = self.operation_outcome(operation)
            if prior is not None:
                return None
            existing = self.operations.lookup_cancel(operation)
            if existing is not None:
                self.check_cancel(existing, expected, checksum)
                return existing
            cancelled = CancelledOutcome(operation, expected, checksum)
            self.operations.record_cancelled(cancelled)
        return cancelled

    @staticmethod
    def check_outcome(outcome: OverwriteOutcome, expected: FrameRef, checksum: str) -> None:
        if (outcome.replaced_occurrence, outcome.replacement.slot, outcome.replacement.checksum) != (
            expected.occurrence_id, expected.slot, checksum,
        ):
            raise RequestConflict(f'overwrite {outcome.operation!r} has a different device outcome')

    @staticmethod
    def check_cancel(cancelled: CancelledOutcome, expected: FrameRef, checksum: str) -> None:
        if (cancelled.expected, cancelled.checksum) != (expected, checksum):
            raise RequestConflict(f'overwrite {cancelled.operation!r} has a different withdrawal')

    def operation_outcome(self, operation: str) -> OverwriteOutcome | None:
        return self.operations.lookup(operation)

    def cancellation_outcome(self, operation: str) -> CancelledOutcome | None:
        return self.operations.lookup_cancel(operation)

    def overwrite_count(self) -> int:
        return self.operations.count()

    def disconnect(self) -> None:
        if not self.db.connection.in_transaction:
            raise RuntimeError('recorder disconnect requires the station close transaction')
        self.slots.set_connected(False)

    @staticmethod
    def install(conn: Connection) -> None:
        DeviceSlotStore.install(conn)
        DeviceOperations.install(conn)
