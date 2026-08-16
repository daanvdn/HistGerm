"""Durable, atomic, append-only run-journal store.

The journal is logically append-only: recorded events are never mutated or
removed, only extended. Each write is made durable and atomic by serializing the
recovered prior events plus the new event to a same-directory temporary that is
flushed, ``fsync``-ed, and atomically renamed into place through the shared
:mod:`histgerm.research._persistence` primitives from ``TASK-MIG-004``. An
interrupted append therefore never corrupts or loses the prior journal: the
partial temporary is discarded and the previous file is left intact.

Reads independently tolerate a single torn trailing line (the signature of an
externally interrupted append) by recovering the valid leading events, while any
mid-file corruption, sequence gap, wrong run identifier, or stale checkpoint hash
is a fatal, non-recoverable error.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ._persistence import (
    bounded_file_lock,
    replace_atomically,
    stable_lock_path,
    write_durable_temporary,
)
from .run_journal import (
    JOURNAL_SCHEMA_VERSION,
    AnyJournalEvent,
    CheckpointEvent,
    CheckpointPayload,
    JournalReplay,
    encode_event,
    encode_events,
    journal_content_hash,
    parse_event,
    replay_journal,
)

JOURNAL_SUFFIX = ".journal.jsonl"
JOURNAL_LOCK_NAMESPACE = "journal-locks-v1"
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 10.0
_PACKAGE_DIR = Path(__file__).resolve().parents[1]


class JournalError(ValueError):
    """Base class for recoverable, caller-translatable journal failures."""


class JournalPathError(JournalError):
    """Report a journal path that is unsafe or wrongly located."""


class JournalConflictError(JournalError):
    """Report an optimistic-concurrency or append-ordering conflict.

    Raised for a wrong run identifier, an optimistic-concurrency mismatch, a
    sequence gap, or a conflicting duplicate (a differing event recorded at an
    already-committed sequence). A byte-identical duplicate append is not a
    conflict: it is an idempotent no-op.
    """


class JournalCorruptionError(JournalError):
    """Report non-recoverable journal corruption or a stale checkpoint."""


class JournalWriteError(OSError):
    """Report failure to durably create or replace the journal."""


@dataclass(frozen=True)
class ParsedJournal:
    """The recovered, integrity-checked contents of one journal file."""

    events: tuple[AnyJournalEvent, ...]
    truncated_tail: bool
    run_id: str | None
    last_sequence: int
    content_hash: str


@dataclass(frozen=True)
class AppendResult:
    """The durable outcome of one append or compaction request."""

    run_id: str
    sequence: int
    last_sequence: int
    event_count: int
    content_hash: str
    idempotent: bool
    kind: str


def validate_journal_path(path: Path, *, option: str) -> Path:
    """Require one safe operational journal path outside trusted package data.

    The file name must end with ``.journal.jsonl`` so the artifact is always
    excluded from wheels and source distributions (``TASK-MIG-001`` globs), the
    path may not be a symlink or an existing non-file, its parent directory must
    exist, and it must never resolve inside the installed ``histgerm`` package
    (which carries the trusted inventory data).
    """

    if not path.name.endswith(JOURNAL_SUFFIX):
        raise JournalPathError(f"{option} must name a *{JOURNAL_SUFFIX} file")
    if path.is_symlink():
        raise JournalPathError(f"{option} must not be a symbolic link")
    if path.exists() and not path.is_file():
        raise JournalPathError(f"{option} must be a regular file")
    parent = path.parent
    if not parent.is_dir():
        raise JournalPathError(f"{option} directory does not exist")
    resolved = parent.resolve() / path.name
    if resolved == _PACKAGE_DIR or _PACKAGE_DIR in resolved.parents:
        raise JournalPathError(f"{option} must be outside the histgerm package")
    return path


def read_journal(path: Path) -> ParsedJournal:
    """Read, recover, and integrity-check one existing journal file."""

    return _parse_journal(_read_journal_bytes(path))


def journal_status(path: Path) -> JournalReplay:
    """Return the deterministic replay of one existing journal file."""

    return replay_journal(read_journal(path).events)


def append_event(
    path: Path,
    event: AnyJournalEvent,
    *,
    expected_last_sequence: int | None = None,
) -> AppendResult:
    """Durably append one typed event, idempotent on ``(run_id, sequence)``.

    A byte-identical event already recorded at its sequence is an idempotent
    no-op. A differing event at a committed sequence, a wrong run identifier, a
    sequence gap, or an ``expected_last_sequence`` mismatch each raises
    :class:`JournalConflictError`. The write is atomic and durable, so an
    interrupted append leaves the prior journal valid.
    """

    if event.schema_version != JOURNAL_SCHEMA_VERSION:
        raise JournalConflictError(
            f"event schema version {event.schema_version} is unsupported; "
            f"expected {JOURNAL_SCHEMA_VERSION}"
        )
    with _journal_lock(path):
        parsed = _load_for_append(path)
        events = parsed.events
        if events and event.run_id != parsed.run_id:
            raise JournalConflictError(
                f"event run id {event.run_id!r} does not match journal run id "
                f"{parsed.run_id!r}"
            )
        if (
            expected_last_sequence is not None
            and expected_last_sequence != parsed.last_sequence
        ):
            raise JournalConflictError(
                f"expected last sequence {expected_last_sequence}, "
                f"found {parsed.last_sequence}"
            )
        if event.sequence <= parsed.last_sequence:
            existing = events[event.sequence]
            if encode_event(existing) == encode_event(event):
                return AppendResult(
                    run_id=existing.run_id,
                    sequence=event.sequence,
                    last_sequence=parsed.last_sequence,
                    event_count=len(events),
                    content_hash=parsed.content_hash,
                    idempotent=True,
                    kind=event.kind,
                )
            raise JournalConflictError(
                f"event at sequence {event.sequence} conflicts with the "
                "already recorded event"
            )
        if event.sequence != parsed.last_sequence + 1:
            raise JournalConflictError(
                f"event sequence {event.sequence} leaves a gap after "
                f"{parsed.last_sequence}"
            )
        if not events and (event.sequence != 0 or event.kind != "run_started"):
            raise JournalConflictError(
                "the first journal event must be run_started at sequence 0"
            )
        return _commit(path, (*events, event), idempotent=False)


def compact_journal(
    path: Path,
    *,
    recorded_at: str | None = None,
    expected_last_sequence: int | None = None,
) -> AppendResult:
    """Append a compact checkpoint retaining the journal hash and last sequence.

    Compaction is idempotent when the journal already ends with a checkpoint of
    its current tail. Otherwise it appends one ``checkpoint`` event whose payload
    records the content hash and last sequence of every prior event, so a later
    resume can verify integrity and skip re-deriving confirmed state.
    """

    with _journal_lock(path):
        parsed = _load_for_append(path)
        events = parsed.events
        if not events:
            raise JournalConflictError("cannot checkpoint an empty journal")
        if parsed.run_id is None:
            raise JournalConflictError("cannot checkpoint a journal without a run")
        if (
            expected_last_sequence is not None
            and expected_last_sequence != parsed.last_sequence
        ):
            raise JournalConflictError(
                f"expected last sequence {expected_last_sequence}, "
                f"found {parsed.last_sequence}"
            )
        if events[-1].kind == "checkpoint":
            return AppendResult(
                run_id=parsed.run_id,
                sequence=parsed.last_sequence,
                last_sequence=parsed.last_sequence,
                event_count=len(events),
                content_hash=parsed.content_hash,
                idempotent=True,
                kind="checkpoint",
            )
        checkpoint = CheckpointEvent(
            run_id=parsed.run_id,
            sequence=parsed.last_sequence + 1,
            recorded_at=recorded_at if recorded_at is not None else _now_iso(),
            payload=CheckpointPayload(
                content_hash=parsed.content_hash,
                last_sequence=parsed.last_sequence,
            ),
        )
        return _commit(path, (*events, checkpoint), idempotent=False)


def _commit(
    path: Path, events: tuple[AnyJournalEvent, ...], *, idempotent: bool
) -> AppendResult:
    _write_events(path, events)
    last = events[-1]
    return AppendResult(
        run_id=last.run_id,
        sequence=last.sequence,
        last_sequence=last.sequence,
        event_count=len(events),
        content_hash=journal_content_hash(events),
        idempotent=idempotent,
        kind=last.kind,
    )


def _write_events(path: Path, events: tuple[AnyJournalEvent, ...]) -> None:
    payload = encode_events(events)
    if len(payload) > MAX_JOURNAL_BYTES:
        raise JournalWriteError(f"journal {path} exceeds the size limit")
    temporary = write_durable_temporary(
        path, payload, prefix=f".{path.name}.", suffix=".tmp", mode=0o600
    )
    try:
        replace_atomically(temporary, path)
    except OSError as error:
        raise JournalWriteError(
            f"could not atomically replace journal {path}"
        ) from error


def _load_for_append(path: Path) -> ParsedJournal:
    if not path.exists():
        return _parse_journal(b"")
    return _parse_journal(_read_journal_bytes(path))


def _read_journal_bytes(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise JournalWriteError(f"journal file is unavailable: {error}") from error
    if size > MAX_JOURNAL_BYTES:
        raise JournalCorruptionError("journal file exceeds the size limit")
    return path.read_bytes()


def _parse_journal(data: bytes) -> ParsedJournal:
    if not data:
        return ParsedJournal((), False, None, -1, journal_content_hash(()))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise JournalCorruptionError("journal file must be UTF-8") from error
    truncated_tail = False
    if text.endswith("\n"):
        lines = text.split("\n")[:-1]
    else:
        segments = text.split("\n")
        lines = segments[:-1]
        if segments[-1]:
            truncated_tail = True
    events = tuple(_parse_line(index, line) for index, line in enumerate(lines))
    _validate_integrity(events)
    _verify_checkpoints(events)
    return ParsedJournal(
        events=events,
        truncated_tail=truncated_tail,
        run_id=events[0].run_id if events else None,
        last_sequence=events[-1].sequence if events else -1,
        content_hash=journal_content_hash(events),
    )


def _parse_line(index: int, line: str) -> AnyJournalEvent:
    if not line.strip():
        raise JournalCorruptionError(f"journal line {index} is blank")
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as error:
        raise JournalCorruptionError(
            f"journal line {index} is not valid JSON"
        ) from error
    if not isinstance(raw, dict):
        raise JournalCorruptionError(f"journal line {index} is not a JSON object")
    try:
        return parse_event(raw)
    except ValidationError as error:
        raise JournalCorruptionError(
            f"journal line {index} is not a valid event"
        ) from error


def _validate_integrity(events: tuple[AnyJournalEvent, ...]) -> None:
    if not events:
        return
    run_id = events[0].run_id
    started = 0
    for index, event in enumerate(events):
        if event.schema_version != JOURNAL_SCHEMA_VERSION:
            raise JournalCorruptionError(
                f"journal event {index} has unsupported schema version "
                f"{event.schema_version}"
            )
        if event.run_id != run_id:
            raise JournalCorruptionError(
                f"journal event {index} has run id {event.run_id!r}, "
                f"expected {run_id!r}"
            )
        if event.sequence != index:
            raise JournalCorruptionError(
                f"journal event {index} has sequence {event.sequence}, expected {index}"
            )
        if event.kind == "run_started":
            started += 1
    if events[0].kind != "run_started":
        raise JournalCorruptionError(
            "the first journal event must be run_started at sequence 0"
        )
    if started != 1:
        raise JournalCorruptionError(
            "a journal must contain exactly one run_started event"
        )


def _verify_checkpoints(events: tuple[AnyJournalEvent, ...]) -> None:
    for index, event in enumerate(events):
        if not isinstance(event, CheckpointEvent):
            continue
        prefix = events[:index]
        expected_hash = journal_content_hash(prefix)
        expected_last = prefix[-1].sequence if prefix else -1
        if event.payload.content_hash != expected_hash:
            raise JournalCorruptionError(
                f"checkpoint at sequence {event.sequence} records a stale content hash"
            )
        if event.payload.last_sequence != expected_last:
            raise JournalCorruptionError(
                f"checkpoint at sequence {event.sequence} records a stale last sequence"
            )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _journal_lock_path(path: Path) -> Path:
    return stable_lock_path(path, namespace=JOURNAL_LOCK_NAMESPACE)


@contextmanager
def _journal_lock(path: Path) -> Iterator[None]:
    with bounded_file_lock(
        _journal_lock_path(path),
        label="journal",
        timeout=_LOCK_TIMEOUT_SECONDS,
        on_timeout=JournalWriteError,
    ):
        yield


__all__ = [
    "JOURNAL_LOCK_NAMESPACE",
    "JOURNAL_SUFFIX",
    "MAX_JOURNAL_BYTES",
    "AppendResult",
    "JournalConflictError",
    "JournalCorruptionError",
    "JournalError",
    "JournalPathError",
    "JournalWriteError",
    "ParsedJournal",
    "append_event",
    "compact_journal",
    "journal_status",
    "read_journal",
    "validate_journal_path",
]
