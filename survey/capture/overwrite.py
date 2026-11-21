from survey.capture.candidates import OverwriteRequest, PollJournal, PollRecord
from survey.capture.custody import CustodyStore
from survey.capture.frontier import EditionFrontier
from survey.device.operations import CancelledOutcome, OverwriteOutcome
from survey.device.recorder import Recorder
from survey.device.slots import FrameRef, FrameUnavailable
from survey.recording.requests import RequestConflict
from survey.recording.transactions import StationDatabase


class ObligationPending(RuntimeError):
    """An original frame still needed by an unsettled candidate cannot be preserved."""


def obligations(journal: PollJournal, frontier: EditionFrontier,
                recorder: Recorder) -> tuple[FrameRef, ...]:
    """Every original identity whose body must stay redeemable.

    That covers the explicit frames of unsettled candidates, the new frames a
    device already wrote but the station has not published, and every frame a
    published edition still references.  Identities are unique station-wide.
    """
    collected: dict[str, FrameRef] = {}
    for record in journal.pending_overwrites():
        for reference in record.declared_references():
            collected[reference.occurrence_id] = reference
        outcome = recorder.operation_outcome(record.operation)
        if outcome is not None:
            collected[outcome.replacement.occurrence_id] = outcome.replacement
    for reference in frontier.referenced_occurrences():
        collected[reference.occurrence_id] = reference
    return tuple(collected.values())


def preserve_slot(custody: CustodyStore, journal: PollJournal, frontier: EditionFrontier,
                  recorder: Recorder, slot: str) -> None:
    """Persist the identity, body and provenance of every obligation in one slot."""
    for reference in obligations(journal, frontier, recorder):
        if reference.slot != slot:
            continue
        try:
            custody.preserve(reference)
        except (FrameUnavailable, ValueError) as error:
            raise ObligationPending(str(error)) from error


def prepare_overwrite(db: StationDatabase, journal: PollJournal, frontier: EditionFrontier,
                      custody: CustodyStore, recorder: Recorder, operation: str,
                      *, reject_commit: bool = False) -> PollRecord:
    with db.atomic():
        record = journal.load(operation)
        if record is None or record.expected is None:
            raise RuntimeError('no overwrite to prepare')
        if record.state != 'staged':
            return record
        preserve_slot(custody, journal, frontier, recorder, record.expected.slot)
        journal.mark_prepared(operation)
        if reject_commit:
            from survey.recording.transactions import CommitRejected
            raise CommitRejected('custody transaction rejected before commit')
    return journal.load(operation)


def apply_overwrite(recorder: Recorder, record: PollRecord, request: OverwriteRequest,
                    *, lose_reply: bool = False) -> OverwriteOutcome:
    if record.state != 'prepared' or record.expected is None:
        raise RuntimeError('overwrite has no committed preparation')
    return recorder.overwrite(record.operation, record.expected, request.amplitude,
                              request.checksum, lose_reply=lose_reply)


def recover_overwrite(recorder: Recorder, record: PollRecord, request: OverwriteRequest,
                      *, lose_reply: bool = False) -> OverwriteOutcome:
    outcome = recorder.operation_outcome(record.operation)
    if outcome is not None:
        recorder.check_outcome(outcome, record.expected, request.checksum)
        return outcome
    return apply_overwrite(recorder, record, request, lose_reply=lose_reply)


def _binding(journal: PollJournal, recording: str, operation: str) -> PollRecord:
    record = journal.load(operation)
    if record is None:
        raise LookupError(f'unknown overwrite operation {operation!r}')
    if record.recording != recording:
        raise RequestConflict(f'overwrite operation {operation!r} is bound to another recording')
    if record.expected is None:
        raise ValueError(f'operation {operation!r} is a plain collection, not an overwrite')
    return record


def settle_cancelled(db: StationDatabase, journal: PollJournal, record: PollRecord) -> None:
    """Persist the station's cancellation terminal for a withdrawn overwrite."""
    with db.atomic():
        current = journal.load(record.operation)
        if current is None:
            existing = journal.load_cancel(record.operation)
            if existing is not None:
                existing.check_request(record.recording, record.manifest)
            return
        current.check_request(record.recording, record.manifest)
        journal.record_cancel(record.operation, record.recording, current.manifest, current.expected)


def withdraw_overwrite(db: StationDatabase, journal: PollJournal, recorder: Recorder,
                       recording: str, operation: str) -> bool:
    """Settle a withdrawal from the device terminal inwards.

    Returns ``True`` when the withdrawal is the device terminal, leaving the
    candidate without a new edition.  Returns ``False`` when the device had
    already written the replacement, so the caller settles it as executed.

    The device terminal is committed on its own so a later station settlement
    failure cannot resurrect the command; a reopen recognizes the terminal and
    settles the original operation number again.
    """
    cancelled = journal.load_cancel(operation)
    if cancelled is not None:
        if cancelled.recording != recording:
            raise RequestConflict(f'overwrite operation {operation!r} is bound to another recording')
        return True
    record = _binding(journal, recording, operation)
    outcome: OverwriteOutcome | None = recorder.operation_outcome(operation)
    if outcome is not None:
        recorder.check_outcome(outcome, record.expected, record.replacement_checksum())
        return False
    terminal: CancelledOutcome | None = recorder.withdraw(
        operation, record.expected, record.replacement_checksum())
    if terminal is None:
        return False
    settle_cancelled(db, journal, record)
    return True


def heal_cancellations(journal: PollJournal, recorder: Recorder) -> None:
    """Move unsettled journal records onto their device cancellation terminal.

    The device terminal is independent of station settlement, so a settlement
    that failed after the device withdrew is reclaimed on the next call under
    the same operation number.
    """
    for record in journal.pending_overwrites():
        if recorder.cancellation_outcome(record.operation) is None:
            continue
        cancelled = journal.load_cancel(record.operation)
        if cancelled is None:
            journal.record_cancel(record.operation, record.recording, record.manifest, record.expected)
        else:
            cancelled.check_request(record.recording, record.manifest)


def has_pending_overwrites(journal: PollJournal) -> bool:
    return bool(journal.pending_overwrites())
