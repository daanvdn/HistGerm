"""Shared durable, atomic, single-writer persistence primitives.

Private module extracting the same-directory temporary write, flush, ``fsync``,
atomic replace, cleanup, and bounded OS-backed lock acquisition shared by the
discovery ledger, the vocabulary store, and the discovery checkpoint writer.
These primitives never fall back silently: every failure re-raises its original
cause so each caller can translate it into its own public error type without
changing any serialized format, exception type, or CLI exit code.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

type MakeError = Callable[[str], BaseException]

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.02


def write_durable_temporary(
    path: Path,
    payload: bytes,
    *,
    prefix: str,
    suffix: str = "",
    mode: int | None = None,
) -> Path:
    """Write ``payload`` to a same-directory temporary and return its path.

    The bytes are flushed and ``fsync``-ed before the descriptor closes, so the
    temporary is durable on disk when this returns. When ``mode`` is given the
    temporary is ``chmod``-ed before the caller renames it into place. On any
    failure the descriptor is closed, the partial temporary removed, and the
    original exception re-raised for the caller to translate.
    """

    descriptor, raw_path = tempfile.mkstemp(
        dir=path.parent, prefix=prefix, suffix=suffix
    )
    temporary = Path(raw_path)
    adopted = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            adopted = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        return temporary
    except BaseException:
        if not adopted:
            os.close(descriptor)
        _silent_unlink(temporary)
        raise


def replace_atomically(temporary: Path, path: Path) -> None:
    """Atomically move ``temporary`` onto ``path`` within the same directory.

    On any failure the temporary is removed and the original exception is
    re-raised so the caller reports its own atomic-replace error while leaving
    the prior file content untouched.
    """

    try:
        os.replace(temporary, path)
    except BaseException:
        _silent_unlink(temporary)
        raise


def remove_temporary(path: Path) -> None:
    """Remove a temporary file, ignoring only its prior absence."""

    path.unlink(missing_ok=True)


def stable_lock_path(target: Path, *, namespace: str) -> Path:
    """Return a deterministic per-target lock path under the system temp dir.

    The lock lives outside the target's directory so it never appears beside
    tracked state, is stable across processes for one resolved target, and is
    distinct for distinct targets and namespaces.
    """

    identity = os.path.normcase(os.fspath(target.resolve()))
    digest = hashlib.sha256(os.fsencode(identity)).hexdigest()
    return Path(tempfile.gettempdir()) / "histgerm" / namespace / f"{digest}.lock"


@contextmanager
def bounded_file_lock(
    lock: Path,
    *,
    label: str,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    on_timeout: MakeError,
) -> Iterator[None]:
    """Hold a bounded, OS-backed exclusive advisory lock on a persistent file.

    The stable lock file is created if absent and never evicted. Because the
    exclusion is an OS advisory lock rather than a lock file's existence, a
    crashed owner leaves no stale lock: the kernel releases the lock when the
    owning process exits and the next writer reclaims it without any unsafe
    automatic deletion. Acquisition is bounded by ``timeout``; on expiry
    ``on_timeout`` builds the raised error naming ``label`` and ``lock``.
    """

    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            locking = getattr(msvcrt, "locking")  # noqa: B009
            lock_nblck = getattr(msvcrt, "LK_NBLCK")  # noqa: B009
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    locking(descriptor, lock_nblck, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise on_timeout(
                            f"timed out waiting for {label} lock {lock}"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
        else:
            import fcntl

            flock = getattr(fcntl, "flock")  # noqa: B009
            lock_ex = getattr(fcntl, "LOCK_EX")  # noqa: B009
            lock_nb = getattr(fcntl, "LOCK_NB")  # noqa: B009
            while True:
                try:
                    flock(descriptor, lock_ex | lock_nb)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise on_timeout(
                            f"timed out waiting for {label} lock {lock}"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            if acquired and os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                getattr(msvcrt, "locking")(  # noqa: B009
                    descriptor,
                    getattr(msvcrt, "LK_UNLCK"),  # noqa: B009
                    1,
                )
            elif acquired:
                getattr(fcntl, "flock")(  # noqa: B009
                    descriptor,
                    getattr(fcntl, "LOCK_UN"),  # noqa: B009
                )
        finally:
            os.close(descriptor)


def _silent_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "MakeError",
    "bounded_file_lock",
    "remove_temporary",
    "replace_atomically",
    "stable_lock_path",
    "write_durable_temporary",
]
