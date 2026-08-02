"""Direct tests for the shared cross-platform process lock."""

from __future__ import annotations

import os

import pytest


def test_process_lock_acquires_and_releases(tmp_path):
    from process_lock import process_lock

    target = tmp_path / "lock.bin"
    with process_lock(target, timeout=0.5):
        assert target.exists()
    with process_lock(target, timeout=0.5):
        pass


@pytest.mark.skipif(os.name == "nt", reason="same-process lock semantics differ on Windows")
def test_process_lock_times_out_when_held(tmp_path):
    from process_lock import process_lock

    target = tmp_path / "lock.bin"
    with process_lock(target, timeout=0.5):
        with pytest.raises(TimeoutError):
            with process_lock(target, timeout=0.2):
                pass
