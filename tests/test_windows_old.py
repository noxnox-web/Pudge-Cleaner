"""Tests for the Windows 11 windows.old removal.

No test deletes a real windows.old: the folder path and Windows-11 detection
are patched, and the handle-based delete runs against a throwaway tree.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from pudge_gaming_manager.core.optimization.tweak import Outcome, TweakContext
from pudge_gaming_manager.core.optimization.tweaks.windows_old import (
    RemoveWindowsOldTweak,
)
from pudge_gaming_manager.utilities.formatting import format_size
from pudge_gaming_manager.windows.cleanup import windows_old

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")


def _fake_old(tmp_path: pathlib.Path, monkeypatch, present: bool = True) -> pathlib.Path:
    old = tmp_path / "Windows.old"
    if present:
        (old / "Windows" / "System32").mkdir(parents=True)
        (old / "Windows" / "System32" / "x.dll").write_bytes(b"x" * 2048)
    monkeypatch.setattr(windows_old, "windows_old_path", lambda: old)
    return old


# -- detection ---------------------------------------------------------------


def test_not_offered_off_windows_11(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_old, "is_windows_11", lambda: False)
    _fake_old(tmp_path, monkeypatch)
    state = RemoveWindowsOldTweak().scan(TweakContext())
    assert state.needs_change is False
    assert "Windows 11" in state.summary


def test_not_offered_without_the_folder(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_old, "is_windows_11", lambda: True)
    _fake_old(tmp_path, monkeypatch, present=False)
    state = RemoveWindowsOldTweak().scan(TweakContext())
    assert state.needs_change is False


def test_offered_on_windows_11_with_the_folder(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_old, "is_windows_11", lambda: True)
    _fake_old(tmp_path, monkeypatch)
    state = RemoveWindowsOldTweak().scan(TweakContext())
    assert state.needs_change is True
    assert "windows.old" in state.summary


# -- removal -----------------------------------------------------------------


def test_apply_removes_the_tree_without_rewriting_permissions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_old, "is_windows_11", lambda: True)
    old = _fake_old(tmp_path, monkeypatch)
    tweak = RemoveWindowsOldTweak()

    result = tweak.apply(TweakContext(), tweak.scan(TweakContext()))

    assert result.outcome is Outcome.SUCCESS, result.detail
    assert "2.0 КБ" in result.detail or "файлов: 1" in result.detail
    assert not old.exists()
    assert tweak.verify(TweakContext(), tweak.scan(TweakContext())).confirmed


def test_apply_reports_progress_against_the_scanned_size(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_old, "is_windows_11", lambda: True)
    _fake_old(tmp_path, monkeypatch)
    tweak = RemoveWindowsOldTweak()
    state = tweak.scan(TweakContext())
    reports: list = []
    ctx = TweakContext(progress=lambda fraction, detail: reports.append((fraction, detail)))

    assert tweak.apply(ctx, state).outcome is Outcome.SUCCESS

    # The delete always ends with a report of its final totals.
    size = format_size(2048)
    assert reports[-1] == (1.0, f"удалено {size} из {size}")


def test_progress_without_a_measured_size_has_no_fraction(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_old, "is_windows_11", lambda: True)
    _fake_old(tmp_path, monkeypatch)
    monkeypatch.setattr(windows_old, "folder_size", lambda: None)
    tweak = RemoveWindowsOldTweak()
    state = tweak.scan(TweakContext())
    reports: list = []
    ctx = TweakContext(progress=lambda fraction, detail: reports.append((fraction, detail)))

    assert tweak.apply(ctx, state).outcome is Outcome.SUCCESS
    assert reports[-1] == (None, f"удалено {format_size(2048)}")


def test_the_size_is_read_by_handle(tmp_path, monkeypatch) -> None:
    _fake_old(tmp_path, monkeypatch)
    assert windows_old.folder_size() == 2048


def test_dry_run_deletes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_old, "is_windows_11", lambda: True)
    old = _fake_old(tmp_path, monkeypatch)
    tweak = RemoveWindowsOldTweak()
    result = tweak.apply(TweakContext(dry_run=True), tweak.scan(TweakContext()))
    assert result.outcome is Outcome.SUCCESS
    assert old.exists()


def test_files_left_behind_are_a_failure_not_a_success(tmp_path, monkeypatch) -> None:
    from pudge_gaming_manager.utilities.tree_delete import DeleteStats

    monkeypatch.setattr(windows_old, "is_windows_11", lambda: True)
    _fake_old(tmp_path, monkeypatch)
    monkeypatch.setattr(
        windows_old, "remove",
        lambda path=None, progress=None: DeleteStats(
            bytes=10, files=1, failed=2, errors=["x: занят"]
        ),
    )
    tweak = RemoveWindowsOldTweak()
    result = tweak.apply(TweakContext(), tweak.scan(TweakContext()))
    assert result.outcome is Outcome.FAILED
    assert "не удалось: 2" in result.detail


@windows_only
def test_a_junction_inside_windows_old_is_deleted_as_a_link(tmp_path, monkeypatch) -> None:
    r"""Windows.old holds junctions such as «Users\All Users»; the old
    takeown /r and icacls /t walked into them and re-permissioned the live
    system. The target must come through untouched."""
    old = _fake_old(tmp_path, monkeypatch)
    live = tmp_path / "ProgramData"
    live.mkdir()
    (live / "settings.ini").write_bytes(b"live")
    (old / "Users").mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(old / "Users" / "All Users"), str(live)],
        check=True, capture_output=True,
    )

    stats = windows_old.remove()

    assert stats is not None and stats.failed == 0 and stats.links == 1
    assert not old.exists()
    assert (live / "settings.ini").read_bytes() == b"live"


@windows_only
def test_a_junction_in_place_of_windows_old_is_refused(tmp_path, monkeypatch) -> None:
    """windows.old swapped for a junction must not be followed."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.dll").write_bytes(b"keep")
    old = tmp_path / "Windows.old"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(old), str(victim)],
        check=True, capture_output=True,
    )
    monkeypatch.setattr(windows_old, "windows_old_path", lambda: old)
    assert windows_old.is_present() is False  # a reparse point is not "present"
    assert windows_old.remove() is None  # refused, not "nothing to do"
    assert (victim / "keep.dll").exists()
