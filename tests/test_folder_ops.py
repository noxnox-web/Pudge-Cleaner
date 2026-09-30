"""Deleting or moving a folder from the usage survey.

Every test runs on a throwaway tree, against a *fake machine*: the Windows,
Program Files, ProgramData and profile locations the policy compares against
are pointed at folders under ``tmp_path``. The policy reads them from the
environment at call time, so the real ``C:\\Windows`` is never involved and a
test cannot delete, move or even look at anything real.

The second drive a move needs does not exist in a test, so the "is this
another volume" check is replaced where a move is exercised, and tested on
its own where it is the point.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

from pudge_gaming_manager.utilities import tree_copy, tree_delete
from pudge_gaming_manager.utilities.junction import create_junction
from pudge_gaming_manager.windows import folder_ops
from pudge_gaming_manager.windows.folder_ops import (
    FolderAction,
    FolderRefused,
    plan_delete,
    plan_move,
    run_folder,
)
from pudge_gaming_manager.windows.folder_policy import block_reason

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")


@pytest.fixture()
def machine(tmp_path: pathlib.Path, monkeypatch) -> SimpleNamespace:
    """A fake Windows layout, and a work area that is not under any of it."""
    base = tmp_path / "machine"
    places = {
        "SystemRoot": base / "Windows",
        "ProgramFiles": base / "Program Files",
        "ProgramFiles(x86)": base / "Program Files (x86)",
        "ProgramW6432": base / "Program Files",
        "ProgramData": base / "ProgramData",
        "USERPROFILE": base / "Users" / "player",
        "APPDATA": base / "Users" / "player" / "AppData" / "Roaming",
        "LOCALAPPDATA": base / "Users" / "player" / "AppData" / "Local",
    }
    for name, path in places.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    work = tmp_path / "work"
    work.mkdir()
    return SimpleNamespace(
        windows=places["SystemRoot"], pf=places["ProgramFiles"],
        pf86=places["ProgramFiles(x86)"], data=places["ProgramData"],
        profile=places["USERPROFILE"], roaming=places["APPDATA"],
        local=places["LOCALAPPDATA"], work=work,
    )


def _folder(path: pathlib.Path, files: int = 3, size: int = 100) -> pathlib.Path:
    (path / "sub").mkdir(parents=True)
    for index in range(files):
        (path / ("sub" if index % 2 else ".") / f"f{index}.bin").write_bytes(b"x" * size)
    return path


@pytest.fixture()
def other_drive(monkeypatch) -> None:
    """Treat every pair of folders as being on different volumes."""
    monkeypatch.setattr(folder_ops, "_other_volume", lambda a, b: True)


# -- what may be touched --------------------------------------------------------


@pytest.mark.parametrize("where", [
    lambda m: m.profile / "Games" / "Big",
    lambda m: m.profile / "Downloads" / "installers" / "old",
    lambda m: m.pf86 / "Steam" / "steamapps" / "common",     # a library: what a person moves
    lambda m: m.pf / "Riot Games" / "League of Legends",
    lambda m: m.profile / "Videos" / "Captures" / "2025",     # the owner's own files
    lambda m: m.local / "Programs" / "SomeApp",
    lambda m: m.data / "Epic" / "Cache",
])
def test_a_folder_a_person_would_choose_is_allowed(machine, where) -> None:
    folder = where(machine)
    folder.mkdir(parents=True)
    assert block_reason(folder) is None


@pytest.mark.parametrize("where", [
    lambda m: m.windows,
    lambda m: m.windows / "System32" / "drivers",
    lambda m: m.profile,
    lambda m: m.pf,
    lambda m: m.roaming,
    lambda m: m.pf / "WindowsApps" / "Some.App_1.0",
    lambda m: m.pf / "Common Files" / "Microsoft Shared",
    lambda m: m.pf / "Windows Defender" / "x",
    lambda m: m.pf / "Microsoft Office" / "root",
    lambda m: m.data / "Microsoft" / "Windows" / "Start Menu",
    lambda m: m.data / "Package Cache" / "x",
    lambda m: m.roaming / "Microsoft" / "Protect" / "S-1-5",        # DPAPI keys
    lambda m: m.local / "Microsoft" / "Edge" / "User Data",
    lambda m: m.local / "Packages" / "Some.App",
    lambda m: m.local / "Temp" / "x",
    lambda m: m.pf / "SmartShell Client" / "x",                    # club software
    lambda m: m.pf / "EasyAntiCheat" / "x",                        # anti-cheat
    lambda m: m.profile / "Games" / "Vault" / "x",                 # credentials
])
def test_what_would_break_the_machine_is_refused(machine, where) -> None:
    folder = where(machine)
    folder.mkdir(parents=True, exist_ok=True)
    assert block_reason(folder), f"{folder} should be refused"


def test_a_drive_root_is_refused() -> None:
    assert block_reason(pathlib.Path("C:/")) is not None


def test_a_missing_folder_and_a_file_are_refused(machine) -> None:
    (machine.work / "f.txt").write_text("x")
    assert block_reason(machine.work / "gone")
    assert block_reason(machine.work / "f.txt")


@windows_only
def test_a_junction_is_refused_as_a_link_not_followed(machine) -> None:
    target = _folder(machine.work / "target")
    create_junction(machine.work / "link", target)
    assert "ссылка" in block_reason(machine.work / "link")


@windows_only
def test_a_folder_reached_through_a_junction_is_judged_by_where_it_is(machine) -> None:
    """``profile\\Games\\x`` looks harmless but is really System32."""
    (machine.windows / "System32").mkdir()
    (machine.profile / "Games").mkdir()
    create_junction(machine.profile / "Games" / "x", machine.windows)

    assert block_reason(machine.profile / "Games" / "x" / "System32") is not None


# -- planning -------------------------------------------------------------------


def test_plan_delete_measures_and_changes_nothing(machine) -> None:
    folder = _folder(machine.work / "Big", files=4, size=250)

    plan = plan_delete(folder)

    assert (plan.action, plan.file_count, plan.size_bytes) == (FolderAction.DELETE, 4, 1000)
    assert folder.exists() and len(list(folder.rglob("*.bin"))) == 4


def test_plan_delete_refuses_the_windows_folder(machine) -> None:
    with pytest.raises(FolderRefused, match="Windows"):
        plan_delete(machine.windows)


def test_a_move_to_the_same_drive_is_refused(machine) -> None:
    source = _folder(machine.work / "Big")
    (machine.work / "dest").mkdir()
    # Nothing patched: the real volume check, on two folders of one drive.
    with pytest.raises(FolderRefused, match="тот же диск"):
        plan_move(source, machine.work / "dest")


def test_the_volume_check_compares_volumes_not_folders(machine) -> None:
    (machine.work / "a").mkdir()
    (machine.work / "b").mkdir()
    assert folder_ops._other_volume(machine.work / "a", machine.work / "b") is False


def test_plan_move_names_the_folder_it_will_create(machine, other_drive) -> None:
    source = _folder(machine.work / "Big", files=2, size=500)
    (machine.work / "dest").mkdir()

    plan = plan_move(source, machine.work / "dest")

    assert plan.action is FolderAction.MOVE
    assert plan.destination == machine.work / "dest" / "Big"
    assert (plan.file_count, plan.size_bytes) == (2, 1000)


def test_a_move_onto_an_existing_folder_is_refused(machine, other_drive) -> None:
    source = _folder(machine.work / "Big")
    (machine.work / "dest" / "Big").mkdir(parents=True)
    with pytest.raises(FolderRefused, match="уже есть"):
        plan_move(source, machine.work / "dest")


def test_a_missing_destination_is_refused(machine, other_drive) -> None:
    source = _folder(machine.work / "Big")
    with pytest.raises(FolderRefused, match="нет"):
        plan_move(source, machine.work / "nowhere")


def test_a_move_into_itself_is_refused(machine, other_drive) -> None:
    source = _folder(machine.work / "Big")
    with pytest.raises(FolderRefused, match="внутрь самой себя"):
        plan_move(source, source / "sub")


def test_a_move_that_does_not_fit_is_refused(machine, other_drive, monkeypatch) -> None:
    source = _folder(machine.work / "Big", size=1000)
    (machine.work / "dest").mkdir()
    monkeypatch.setattr(
        folder_ops.shutil, "disk_usage", lambda _p: SimpleNamespace(free=1000)
    )
    with pytest.raises(FolderRefused, match="свободно"):
        plan_move(source, machine.work / "dest")


@windows_only
def test_a_folder_holding_a_link_is_not_moved(machine, other_drive) -> None:
    source = _folder(machine.work / "Big")
    create_junction(source / "lnk", _folder(machine.work / "elsewhere"))
    (machine.work / "dest").mkdir()

    with pytest.raises(FolderRefused, match="ссылок"):
        plan_move(source, machine.work / "dest")


# -- deleting -------------------------------------------------------------------


def test_delete_removes_the_folder_and_reports_progress(machine) -> None:
    folder = _folder(machine.work / "Big", files=6, size=1000)
    events: list = []

    result = run_folder(plan_delete(folder), events.append)

    assert not folder.exists()
    assert (result.size_bytes, result.file_count) == (6000, 6)
    assert result.complete and result.leftover is None
    assert events and events[-1].fraction == 1.0
    assert [e.fraction for e in events] == sorted(e.fraction for e in events)


def test_a_plan_is_not_a_permission_slip(machine, monkeypatch) -> None:
    """Approved when planned, refused when run: the machine changed between."""
    folder = _folder(machine.work / "Big")
    plan = plan_delete(folder)
    monkeypatch.setenv("SystemRoot", str(machine.work))  # now it is "inside Windows"

    with pytest.raises(FolderRefused, match="Windows"):
        run_folder(plan)
    assert folder.exists()


@windows_only
def test_a_folder_swapped_for_a_junction_after_planning_is_not_followed(machine) -> None:
    folder = _folder(machine.work / "Big")
    plan = plan_delete(folder)
    victim = _folder(machine.work / "victim")
    tree_delete.delete_tree(folder)
    create_junction(folder, victim)

    with pytest.raises(FolderRefused, match="ссылка"):
        run_folder(plan)
    assert len(list(victim.rglob("*.bin"))) == 3


@windows_only
def test_a_junction_inside_is_deleted_as_a_link(machine) -> None:
    folder = _folder(machine.work / "Big")
    live = _folder(machine.work / "live")
    create_junction(folder / "lnk", live)

    result = run_folder(plan_delete(folder))

    assert not folder.exists()
    assert len(list(live.rglob("*.bin"))) == 3  # the target came through untouched
    assert result.complete


@windows_only
def test_a_locked_file_is_left_and_reported(machine) -> None:
    folder = _folder(machine.work / "Big")
    handle = open(folder / "f0.bin", "r+b")
    try:
        result = run_folder(plan_delete(folder))
    finally:
        handle.close()

    assert not result.complete
    assert result.leftover == folder and result.failed
    assert (folder / "f0.bin").exists()


# -- moving ---------------------------------------------------------------------


def _moved(machine, source: pathlib.Path, events=None):
    (machine.work / "dest").mkdir(exist_ok=True)
    plan = plan_move(source, machine.work / "dest")
    return plan, run_folder(plan, events.append if events is not None else None)


@windows_only
def test_a_move_copies_links_the_old_path_and_removes_the_original(machine, other_drive) -> None:
    source = _folder(machine.work / "Big", files=5, size=2000)
    before = sorted(p.name for p in source.rglob("*.bin"))
    events: list = []

    plan, result = _moved(machine, source, events)

    assert os.path.isjunction(source)  # the old path still works
    assert sorted(p.name for p in source.rglob("*.bin")) == before  # ...and reads through
    assert sorted(p.name for p in plan.destination.rglob("*.bin")) == before
    assert not list(machine.work.glob("*.pgm-*"))  # nothing renamed is left lying about
    assert (result.size_bytes, result.file_count) == (10000, 5)
    assert result.linked and result.complete and result.leftover is None
    texts = " ".join(e.text for e in events)
    assert "копирование" in texts and "удаление старой копии" in texts


@windows_only
def test_a_folder_in_use_is_refused_before_anything_is_copied(machine, other_drive) -> None:
    source = _folder(machine.work / "Big")
    plan = (machine.work / "dest").mkdir() or plan_move(source, machine.work / "dest")
    handle = open(source / "f0.bin", "r+b")  # a program has it open
    try:
        with pytest.raises(FolderRefused, match="переименовать"):
            run_folder(plan)
    finally:
        handle.close()

    assert not (machine.work / "dest" / "Big").exists()  # nothing was copied
    assert (source / "f0.bin").exists() and not os.path.isjunction(source)


@windows_only
def test_a_failed_copy_leaves_the_original_and_removes_the_partial_copy(
    machine, other_drive, monkeypatch
) -> None:
    source = _folder(machine.work / "Big")
    (machine.work / "dest").mkdir()
    plan = plan_move(source, machine.work / "dest")

    def dies_half_way(src, dst, progress=None):
        os.makedirs(dst / "half")
        (dst / "half" / "x.bin").write_bytes(b"partial")
        raise OSError(112, "На диске недостаточно места")

    monkeypatch.setattr(tree_copy, "copy_tree", dies_half_way)

    with pytest.raises(FolderRefused, match="недостаточно места"):
        run_folder(plan)

    assert not (machine.work / "dest" / "Big").exists()
    assert len(list(source.rglob("*.bin"))) == 3 and not os.path.isjunction(source)


@windows_only
def test_a_folder_that_changed_while_copying_is_not_replaced(machine, other_drive, monkeypatch) -> None:
    source = _folder(machine.work / "Big")
    (machine.work / "dest").mkdir()
    plan = plan_move(source, machine.work / "dest")
    real_copy = tree_copy.copy_tree

    def a_program_writes_meanwhile(src, dst, progress=None):
        stats = real_copy(src, dst, progress=progress)
        (src / "written-during-copy.bin").write_bytes(b"new")
        return stats

    monkeypatch.setattr(tree_copy, "copy_tree", a_program_writes_meanwhile)

    with pytest.raises(FolderRefused, match="изменилась"):
        run_folder(plan)

    assert (source / "written-during-copy.bin").exists()  # the original, with its new file
    assert not (machine.work / "dest" / "Big").exists()
    assert not os.path.isjunction(source)


@windows_only
def test_a_junction_that_cannot_be_made_puts_the_original_back(machine, other_drive, monkeypatch) -> None:
    source = _folder(machine.work / "Big")
    (machine.work / "dest").mkdir()
    plan = plan_move(source, machine.work / "dest")

    def refuses(_link, _target):
        raise OSError(5, "Отказано в доступе")

    monkeypatch.setattr(folder_ops, "create_junction", refuses)

    with pytest.raises(FolderRefused, match="ссылку"):
        run_folder(plan)

    assert len(list(source.rglob("*.bin"))) == 3 and not os.path.isjunction(source)
    assert not (machine.work / "dest" / "Big").exists()
    assert not list(machine.work.glob("*.pgm-*"))


@windows_only
def test_if_the_original_cannot_come_back_both_copies_are_kept_and_named(
    machine, other_drive, monkeypatch
) -> None:
    source = _folder(machine.work / "Big")
    (machine.work / "dest").mkdir()
    plan = plan_move(source, machine.work / "dest")
    real_rename = os.rename
    renames = {"n": 0}

    def fourth_rename_fails(src, dst):
        # 1 probe away, 2 probe back, 3 original aside, 4 original back.
        renames["n"] += 1
        if renames["n"] == 4:
            raise OSError(5, "Отказано в доступе")
        return real_rename(src, dst)

    def junction_fails(*_args):
        raise OSError(5, "Отказано в доступе")

    monkeypatch.setattr(folder_ops.os, "rename", fourth_rename_fails)
    monkeypatch.setattr(folder_ops, "create_junction", junction_fails)

    with pytest.raises(FolderRefused) as caught:
        run_folder(plan)

    message = caught.value.user_message()
    assert ".pgm-old-" in message and str(machine.work / "dest" / "Big") in message
    assert (machine.work / "dest" / "Big").is_dir()  # the copy was not discarded
    assert len(list(machine.work.glob("Big.pgm-old-*"))) == 1  # the original is there


@windows_only
def test_files_that_cannot_be_deleted_after_the_move_are_reported(machine, other_drive, monkeypatch) -> None:
    source = _folder(machine.work / "Big")
    (machine.work / "dest").mkdir()
    plan = plan_move(source, machine.work / "dest")
    real_delete = tree_delete.delete_tree

    def leaves_the_old_copy(root, **kwargs):
        if ".pgm-old-" in pathlib.Path(root).name:
            return tree_delete.DeleteStats(failed=1, errors=["f0.bin: занят"])
        return real_delete(root, **kwargs)

    monkeypatch.setattr(tree_delete, "delete_tree", leaves_the_old_copy)

    result = run_folder(plan)

    assert result.linked and not result.complete
    assert result.leftover is not None and ".pgm-old-" in result.leftover.name
    assert result.failed == ["f0.bin: занят"]
    assert os.path.isjunction(source)  # the move itself succeeded


# -- the audit trail ---------------------------------------------------------------


def test_what_was_done_is_written_to_the_audit_log(machine, monkeypatch) -> None:
    audited: list = []
    monkeypatch.setattr(
        folder_ops, "audit_event",
        lambda module, operation, **kw: audited.append((module, operation, kw["result"])),
    )
    run_folder(plan_delete(_folder(machine.work / "Big")))
    assert audited == [("folders", "delete", "SUCCESS")]


def test_a_refusal_is_audited_too(machine, monkeypatch) -> None:
    audited: list = []
    monkeypatch.setattr(
        folder_ops, "audit_event",
        lambda module, operation, **kw: audited.append((operation, kw["result"])),
    )
    folder = _folder(machine.work / "Big")
    plan = plan_delete(folder)
    monkeypatch.setenv("SystemRoot", str(machine.work))
    with pytest.raises(FolderRefused):
        run_folder(plan)
    assert audited == [("delete", "FAILED")]
