"""Copying a tree, making a junction, and the shared progress helpers.

These run against throwaway trees on the real filesystem: the behaviour under
test — junctions, alternate data streams, paths over 260 characters — is the
filesystem's, and a fake would only prove the fake.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

from pudge_gaming_manager.utilities import progress as progress_module
from pudge_gaming_manager.utilities import tree_copy, tree_delete
from pudge_gaming_manager.utilities.junction import create_junction
from pudge_gaming_manager.utilities.progress import (
    Progress,
    Throttle,
    fraction_of,
    guarded,
)
from pudge_gaming_manager.utilities.tree_copy import TreeCopyError, copy_tree, extended

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")


# -- progress helpers ---------------------------------------------------------


def test_fraction_is_clamped_and_absent_without_a_total() -> None:
    assert fraction_of(5, 10) == 0.5
    assert fraction_of(15, 10) == 1.0  # the files grew since the scan
    assert fraction_of(-1, 10) == 0.0
    assert fraction_of(5, 0) is None  # no total, so no number to claim


def test_a_failing_report_is_swallowed_not_raised() -> None:
    def gone(_event: Progress) -> None:
        raise RuntimeError("the window is already closed")

    guarded(gone)(Progress("x"))  # must not raise


def test_throttle_lets_one_report_through_per_interval(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(progress_module.time, "monotonic", lambda: now[0])
    throttle = Throttle(0.25)

    assert not throttle.ready()  # just created: too soon
    now[0] += 0.3
    assert throttle.ready()
    assert not throttle.ready()  # the same instant again
    now[0] += 0.1
    assert not throttle.ready()
    now[0] += 0.2
    assert throttle.ready()


# -- junction ----------------------------------------------------------------


@windows_only
def test_a_junction_reads_through_to_its_target(tmp_path: pathlib.Path) -> None:
    # %TEMP% and & would be mangled by mklink's cmd parser; a direct reparse
    # point takes them literally.
    target = tmp_path / "real %TEMP% & co"
    target.mkdir()
    (target / "f.txt").write_text("hello")

    create_junction(tmp_path / "link", target)

    assert os.path.isjunction(tmp_path / "link")
    assert (tmp_path / "link" / "f.txt").read_text() == "hello"


@windows_only
def test_a_junction_never_replaces_what_is_there(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "t"
    target.mkdir()
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "keep.txt").write_text("mine")

    with pytest.raises(FileExistsError):
        create_junction(tmp_path / "taken", target)
    assert (tmp_path / "taken" / "keep.txt").read_text() == "mine"


@windows_only
def test_a_junction_to_nothing_leaves_no_half_made_folder(tmp_path: pathlib.Path) -> None:
    with pytest.raises(OSError):
        create_junction(tmp_path / "link", tmp_path / "missing")
    assert not (tmp_path / "link").exists()


@windows_only
def test_removing_a_junction_leaves_its_target(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "t"
    target.mkdir()
    (target / "f.txt").write_text("x")
    create_junction(tmp_path / "link", target)

    os.rmdir(tmp_path / "link")

    assert (target / "f.txt").exists()


# -- copy ----------------------------------------------------------------------


def _tree(root: pathlib.Path) -> pathlib.Path:
    (root / "a" / "b").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "small.txt").write_text("hello")
    (root / "a" / "big.bin").write_bytes(os.urandom(1024) * 4096)  # 4 MB
    return root


def test_copy_reproduces_contents_and_counts(tmp_path: pathlib.Path) -> None:
    source = _tree(tmp_path / "src")

    stats = copy_tree(source, tmp_path / "dst")

    assert (tmp_path / "dst" / "small.txt").read_text() == "hello"
    assert (tmp_path / "dst" / "a" / "big.bin").read_bytes() == (source / "a" / "big.bin").read_bytes()
    assert (tmp_path / "dst" / "empty").is_dir()
    assert (stats.files, stats.directories, stats.bytes) == (2, 4, 5 + 4 * 1024 * 1024)


def test_copy_never_overwrites(tmp_path: pathlib.Path) -> None:
    source = _tree(tmp_path / "src")
    (tmp_path / "dst").mkdir()
    (tmp_path / "dst" / "mine.txt").write_text("keep")

    with pytest.raises(FileExistsError):
        copy_tree(source, tmp_path / "dst")
    assert (tmp_path / "dst" / "mine.txt").read_text() == "keep"


def test_progress_rises_to_the_total(tmp_path: pathlib.Path, monkeypatch) -> None:
    # Every report, not one per quarter second, so the chunks of a large
    # file are visible.
    monkeypatch.setattr(tree_copy, "Throttle", lambda *a, **k: SimpleNamespace(ready=lambda: True))
    source = _tree(tmp_path / "src")
    seen: list[int] = []

    stats = copy_tree(source, tmp_path / "dst", progress=seen.append)

    assert seen == sorted(seen)  # never goes backwards
    assert seen[-1] == stats.bytes
    assert len(seen) > 2  # the 4 MB file reported while it was being copied


@windows_only
def test_copy_keeps_attributes_times_and_streams(tmp_path: pathlib.Path) -> None:
    source = _tree(tmp_path / "src")
    with open(str(source / "small.txt") + ":Zone.Identifier", "w") as stream:
        stream.write("[ZoneTransfer]\nZoneId=3\n")
    os.utime(source / "small.txt", (946684800, 946684800))  # after the stream: writing it bumps the time
    (source / "small.txt").chmod(0o444)
    ctypes.windll.kernel32.SetFileAttributesW(str(source / "a" / "b"), 0x2)  # hidden

    copy_tree(source, tmp_path / "dst")
    dst = tmp_path / "dst"

    assert int(os.stat(dst / "small.txt").st_mtime) == 946684800
    assert not os.access(dst / "small.txt", os.W_OK)  # still read-only
    assert os.stat(dst / "a" / "b").st_file_attributes & 0x2  # still hidden
    assert open(str(dst / "small.txt") + ":Zone.Identifier").read().startswith("[ZoneTransfer]")


@windows_only
def test_copy_reaches_paths_beyond_260_characters(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "src"
    deep = source
    for index in range(12):
        deep = deep / ("d" * 25 + str(index))
    os.makedirs(extended(deep))
    with open(extended(deep / "deep.txt"), "w") as handle:
        handle.write("deep")
    assert len(str(deep / "deep.txt")) > 300

    copy_tree(source, tmp_path / "dst")

    assert open(extended(tmp_path / "dst" / deep.relative_to(source) / "deep.txt")).read() == "deep"


@windows_only
def test_a_link_inside_stops_the_copy(tmp_path: pathlib.Path) -> None:
    source = _tree(tmp_path / "src")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(source / "lnk"), str(outside)],
                   check=True, capture_output=True)

    with pytest.raises(TreeCopyError, match="link"):
        copy_tree(source, tmp_path / "dst")

    # The junction's target was never read into the destination.
    assert not list((tmp_path / "dst").rglob("secret.txt"))


@windows_only
def test_a_link_in_place_of_the_source_is_refused(tmp_path: pathlib.Path) -> None:
    real = _tree(tmp_path / "real")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "src"), str(real)],
                   check=True, capture_output=True)

    with pytest.raises(TreeCopyError, match="not a plain directory"):
        copy_tree(tmp_path / "src", tmp_path / "dst")
    assert not (tmp_path / "dst").exists()


# -- measuring ---------------------------------------------------------------


@windows_only
def test_measuring_counts_links_without_following_them(tmp_path: pathlib.Path) -> None:
    source = _tree(tmp_path / "src")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 10_000)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(source / "lnk"), str(outside)],
                   check=True, capture_output=True)

    stats = tree_delete.measure_tree(source)

    assert stats.links == 1
    assert stats.files == 2 and stats.bytes == 5 + 4 * 1024 * 1024  # the target is not counted
