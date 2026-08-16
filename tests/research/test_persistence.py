from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from histgerm.research._persistence import (
    bounded_file_lock,
    remove_temporary,
    replace_atomically,
    stable_lock_path,
    write_durable_temporary,
)

_HOLD_SCRIPT = (
    "import sys, time;"
    "from pathlib import Path;"
    "from histgerm.research._persistence import bounded_file_lock;"
    "ctx = bounded_file_lock("
    "Path(sys.argv[1]), label='probe', on_timeout=RuntimeError);"
    "ctx.__enter__();"
    "print('ready', flush=True);"
    "time.sleep(30)"
)


class _Boom(OSError):
    """Synthetic filesystem failure used to exercise cleanup paths."""


def _raise_boom(*_: object) -> None:
    raise _Boom("synthetic failure")


def test_durable_temporary_writes_flushes_fsyncs_and_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.bin"
    fsynced: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(descriptor: int) -> None:
        fsynced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    temporary = write_durable_temporary(
        target, b"payload", prefix=".state.", suffix=".tmp", mode=0o600
    )
    assert temporary.parent == tmp_path
    assert temporary.read_bytes() == b"payload"
    assert fsynced
    if os.name == "posix":
        assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
    replace_atomically(temporary, target)
    assert target.read_bytes() == b"payload"
    assert not list(tmp_path.glob(".state.*.tmp"))


def test_durable_temporary_cleans_up_and_reraises_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.bin"
    monkeypatch.setattr(os, "fsync", _raise_boom)
    with pytest.raises(_Boom, match="synthetic failure"):
        write_durable_temporary(target, b"payload", prefix=".state.", suffix=".tmp")
    assert not list(tmp_path.iterdir())


def test_replace_atomically_preserves_prior_content_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.bin"
    target.write_bytes(b"original")
    temporary = write_durable_temporary(
        target, b"replacement", prefix=".state.", suffix=".tmp"
    )
    monkeypatch.setattr(os, "replace", _raise_boom)
    with pytest.raises(_Boom, match="synthetic failure"):
        replace_atomically(temporary, target)
    assert target.read_bytes() == b"original"
    assert not temporary.exists()


def test_remove_temporary_tolerates_absence(tmp_path: Path) -> None:
    victim = tmp_path / "scratch.tmp"
    victim.write_bytes(b"x")
    remove_temporary(victim)
    assert not victim.exists()
    remove_temporary(victim)


def test_stable_lock_path_is_deterministic_distinct_and_external(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ledger.yaml"
    lock = stable_lock_path(target, namespace="probe-v1")
    assert lock == stable_lock_path(target, namespace="probe-v1")
    assert lock != stable_lock_path(tmp_path / "other.yaml", namespace="probe-v1")
    assert lock != stable_lock_path(target, namespace="probe-v2")
    assert lock.parent != tmp_path


def test_bounded_lock_reclaims_after_owner_crash_without_deletion(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "probe.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SCRIPT, str(lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    inode_before = lock.stat().st_ino
    holder.kill()
    holder.wait(timeout=5)
    with bounded_file_lock(lock, label="probe", timeout=10, on_timeout=RuntimeError):
        assert lock.exists()
    assert lock.exists()
    assert lock.stat().st_ino == inode_before


def test_bounded_lock_times_out_against_a_live_owner(tmp_path: Path) -> None:
    lock = tmp_path / "probe.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SCRIPT, str(lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    started = time.monotonic()
    with (
        pytest.raises(TimeoutError, match="timed out waiting for probe lock"),
        bounded_file_lock(lock, label="probe", timeout=0.2, on_timeout=TimeoutError),
    ):
        pass
    assert time.monotonic() - started < 2
    holder.kill()
    holder.wait(timeout=5)
