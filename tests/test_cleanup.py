"""Tests for the cleanup engine.

Every test runs against a throwaway directory tree built by the fixtures.
Nothing here points at a real system path (rule #48) — the engine's whole
job is deleting files, so it is exactly the component that must never be
tested against the live machine.
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest

from pudge_gaming_manager.windows.cleanup.engine import CleanupEngine
from pudge_gaming_manager.utilities.formatting import format_size
from pudge_gaming_manager.windows.cleanup.categories import (
    CATEGORIES,
    default_categories,
)
from pudge_gaming_manager.windows.cleanup.rules import (
    CleanupCategory,
    CleanupRisk,
    waived_fragments,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")


def _category(root: pathlib.Path, **kwargs) -> CleanupCategory:
    """A category pointed at a test directory via a temporary env var."""
    os.environ["PGM_TEST_ROOT"] = str(root)
    defaults = {
        "id": "test.category",
        "name": "Test category",
        "description": "Temporary files created by the test suite.",
        "rationale": "Exists only inside a pytest temporary directory.",
        "roots": ("%PGM_TEST_ROOT%",),
        "min_age_hours": 0.0,
        "risk": CleanupRisk.SAFE,
    }
    defaults.update(kwargs)
    return CleanupCategory(**defaults)  # type: ignore[arg-type]


def _write(path: pathlib.Path, content: str = "x" * 100, age_hours: float = 0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
    return path


# -- scanning --------------------------------------------------------------


def test_scan_finds_files_and_totals_their_size(tmp_path: pathlib.Path) -> None:
    _write(tmp_path / "a.tmp", "x" * 100)
    _write(tmp_path / "b.tmp", "x" * 200)

    report = CleanupEngine((_category(tmp_path),)).scan()

    assert report.total_files == 2
    assert report.total_bytes == 300


def test_scan_mutates_nothing(tmp_path: pathlib.Path) -> None:
    target = _write(tmp_path / "a.tmp")
    CleanupEngine((_category(tmp_path),)).scan()
    assert target.exists()


def test_scan_recurses_by_default(tmp_path: pathlib.Path) -> None:
    _write(tmp_path / "deep" / "nested" / "a.tmp")
    report = CleanupEngine((_category(tmp_path),)).scan()
    assert report.total_files == 1


def test_non_recursive_category_stays_shallow(tmp_path: pathlib.Path) -> None:
    _write(tmp_path / "top.tmp")
    _write(tmp_path / "deep" / "nested.tmp")

    category = _category(tmp_path, recursive=False)
    report = CleanupEngine((category,)).scan()

    assert report.total_files == 1


def test_patterns_restrict_what_is_matched(tmp_path: pathlib.Path) -> None:
    _write(tmp_path / "keep.txt")
    _write(tmp_path / "drop.log")

    category = _category(tmp_path, patterns=("*.log",))
    report = CleanupEngine((category,)).scan()

    assert report.total_files == 1
    assert report.categories[0].items[0].path.name == "drop.log"


def test_missing_root_is_reported_not_crashed(tmp_path: pathlib.Path) -> None:
    category = _category(tmp_path / "does-not-exist")
    report = CleanupEngine((category,)).scan()

    assert not report.categories[0].available
    assert "нет" in report.categories[0].unavailable_reason


# -- age gate --------------------------------------------------------------


def test_files_younger_than_the_threshold_are_skipped(
    tmp_path: pathlib.Path,
) -> None:
    """A file written seconds ago is probably open right now."""
    _write(tmp_path / "fresh.tmp", age_hours=0.0)
    _write(tmp_path / "stale.tmp", age_hours=48.0)

    category = _category(tmp_path, min_age_hours=24.0)
    report = CleanupEngine((category,)).scan()

    assert report.total_files == 1
    assert report.categories[0].items[0].path.name == "stale.tmp"
    assert report.categories[0].skipped_too_new == 1


# -- protection ------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment", ["SmartShell", "steamapps", "Documents", "EasyAntiCheat", "Vanguard"]
)
def test_protected_directories_are_never_collected(
    tmp_path: pathlib.Path, fragment: str
) -> None:
    _write(tmp_path / fragment / "important.tmp")
    _write(tmp_path / "ordinary" / "junk.tmp")

    report = CleanupEngine((_category(tmp_path),)).scan()

    collected = [i.path.name for i in report.categories[0].items]
    assert collected == ["junk.tmp"]


@pytest.mark.parametrize("extension", [".sav", ".docx", ".kdbx", ".exe", ".pem"])
def test_protected_extensions_are_never_collected(
    tmp_path: pathlib.Path, extension: str
) -> None:
    _write(tmp_path / f"precious{extension}")
    _write(tmp_path / "junk.tmp")

    report = CleanupEngine((_category(tmp_path),)).scan()

    assert [i.path.name for i in report.categories[0].items] == ["junk.tmp"]


def test_admin_only_category_is_skipped_without_elevation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pudge_gaming_manager.windows.cleanup.engine as mod

    monkeypatch.setattr(mod, "is_admin", lambda: False)
    _write(tmp_path / "a.tmp")

    category = _category(tmp_path, requires_admin=True)
    report = CleanupEngine((category,)).scan()

    assert not report.categories[0].available
    assert "администратора" in report.categories[0].unavailable_reason


# -- containment (the junction-escape defence) ----------------------------


@windows_only
def test_directory_link_is_not_followed_out_of_the_root(
    tmp_path: pathlib.Path,
) -> None:
    """A link inside Temp must not let the cleaner enumerate elsewhere."""
    sandbox = tmp_path / "sandbox"
    outside = tmp_path / "outside"
    sandbox.mkdir()
    _write(outside / "precious.tmp")

    try:
        (sandbox / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a directory symlink in this environment")

    report = CleanupEngine((_category(sandbox),)).scan()

    collected = [str(i.path) for i in report.categories[0].items]
    assert not any("precious" in c for c in collected), collected
    assert (outside / "precious.tmp").exists()


def test_containment_check_rejects_paths_outside_the_root(
    tmp_path: pathlib.Path,
) -> None:
    engine = CleanupEngine(())
    inside = tmp_path / "root" / "file.tmp"
    _write(inside)

    assert engine._contained(inside, tmp_path / "root")
    assert not engine._contained(inside, tmp_path / "elsewhere")


# -- deletion --------------------------------------------------------------


def test_dry_run_deletes_nothing_but_reports_the_total(
    tmp_path: pathlib.Path,
) -> None:
    target = _write(tmp_path / "a.tmp", "x" * 500)

    engine = CleanupEngine((_category(tmp_path),))
    result = engine.clean(engine.scan(), dry_run=True)

    assert target.exists()
    assert result.deleted_files == 1
    assert result.deleted_bytes == 500


def test_clean_removes_approved_files(tmp_path: pathlib.Path) -> None:
    first = _write(tmp_path / "a.tmp")
    second = _write(tmp_path / "sub" / "b.tmp")

    engine = CleanupEngine((_category(tmp_path),))
    result = engine.clean(engine.scan())

    assert not first.exists()
    assert not second.exists()
    assert result.deleted_files == 2
    assert result.failed_files == 0


def test_clean_never_removes_the_root_itself(tmp_path: pathlib.Path) -> None:
    _write(tmp_path / "a.tmp")

    engine = CleanupEngine((_category(tmp_path),))
    engine.clean(engine.scan())

    assert tmp_path.is_dir()


def test_empty_subdirectories_are_removed(tmp_path: pathlib.Path) -> None:
    _write(tmp_path / "sub" / "a.tmp")

    engine = CleanupEngine((_category(tmp_path),))
    engine.clean(engine.scan())

    assert not (tmp_path / "sub").exists()
    assert tmp_path.is_dir()


def test_protected_file_survives_a_clean(tmp_path: pathlib.Path) -> None:
    save = _write(tmp_path / "game.sav")
    junk = _write(tmp_path / "junk.tmp")

    engine = CleanupEngine((_category(tmp_path),))
    engine.clean(engine.scan())

    assert save.exists(), "a protected file was deleted"
    assert not junk.exists()


def test_deletion_revalidates_and_refuses_a_changed_file(
    tmp_path: pathlib.Path,
) -> None:
    """A stale inventory is not permission to delete.

    The file is rewritten after the scan, which under an age gate means
    something is using it now.
    """
    target = _write(tmp_path / "a.tmp", age_hours=48.0)

    category = _category(tmp_path, min_age_hours=24.0)
    engine = CleanupEngine((category,))
    report = engine.scan()
    assert report.total_files == 1

    # Something touches the file between scan and clean.
    target.write_text("in use now", encoding="utf-8")

    result = engine.clean(report)

    assert target.exists()
    assert result.refused_files == 1
    assert result.deleted_files == 0


def test_locked_file_is_counted_not_crashed(tmp_path: pathlib.Path) -> None:
    target = _write(tmp_path / "locked.tmp")
    engine = CleanupEngine((_category(tmp_path),))
    report = engine.scan()

    handle = open(target, "r+", encoding="utf-8")
    try:
        result = engine.clean(report)
    finally:
        handle.close()

    if os.name == "nt":
        # Windows refuses to unlink an open file; that must be survivable.
        assert result.failed_files == 1
        assert target.exists()
    else:
        assert result.deleted_files == 1


# -- the shipped rules -----------------------------------------------------


def test_every_shipped_category_explains_itself() -> None:
    """Same standard as a tweak: no unexplained deletion (rule #64)."""
    for category in CATEGORIES:
        assert category.rationale, f"{category.id} has no rationale"
        assert category.description, f"{category.id} has no description"
        assert category.roots, f"{category.id} has no root"


def test_browser_caches_are_on_by_default() -> None:
    """They clear cache directories only, so nobody gets signed out.

    These were once off by default on the theory that clearing browser data
    ends a player's session. It does not: the roots name cache directories
    and nothing else, which the test below enforces. Leaving them off meant
    the largest reclaimable thing on a club PC was never cleared.
    """
    enabled = {c.id for c in default_categories()}
    browsers = {c.id for c in CATEGORIES if c.id.startswith("cache.browser.")}
    assert browsers <= enabled


def test_categories_holding_user_files_are_off_by_default() -> None:
    """Deleting someone's downloads is never the default."""
    for category in CATEGORIES:
        if category.clears_user_files:
            assert not category.enabled_by_default, (
                f"{category.id} clears user files and must be opt-in"
            )
            assert category.risk is CleanupRisk.MEDIUM


def test_browser_rules_exclude_cookies_and_logins() -> None:
    """Rule #46: say exactly what browser data is removed, and remove only that."""
    browser = [c for c in CATEGORIES if c.id.startswith("cache.browser.")]
    assert browser
    for category in browser:
        joined = " ".join(category.roots).lower()
        for forbidden in ("cookies", "login data", "history", "web data"):
            assert forbidden not in joined, f"{category.id} touches {forbidden}"


def test_no_shipped_category_points_at_a_protected_root() -> None:
    """Each category's own roots must survive its own guard.

    A category whose root is refused finds nothing and reports itself
    empty, which looks identical to "already clean" and is the worst
    failure mode available.
    """
    engine = CleanupEngine(())
    for category in CATEGORIES:
        allowed = waived_fragments(category)
        for root in category.roots:
            expanded = pathlib.Path(os.path.expandvars(root))
            if "%" in str(expanded):
                continue
            assert not engine.is_protected(
                expanded,
                allow_user_files=category.clears_user_files,
                allowed_fragments=allowed,
            ), f"{category.id} -> {expanded}"


def test_a_waiver_must_be_justified_by_the_category_own_roots() -> None:
    """A category cannot waive a fragment its roots never mention.

    This is what keeps ``allowed_path_fragments`` from becoming a general
    escape hatch: waiving "steamapps" from a launcher-log category would
    fail here, because none of its roots contains that name.
    """
    for category in CATEGORIES:
        joined = " ".join(category.roots).lower()
        for fragment in category.allowed_path_fragments:
            assert fragment.lower() in joined, (
                f"{category.id} waives {fragment!r}, which none of its "
                f"roots names"
            )


def test_a_waiver_does_not_unlock_the_other_fragments() -> None:
    """Waiving one vendor name leaves every other guard in place."""
    engine = CleanupEngine(())
    steam_path = pathlib.Path(r"C:\Games\steamapps\common\Dota 2\game.dat")

    assert engine.is_protected(steam_path, allowed_fragments=frozenset({"battle.net"}))
    assert engine.is_protected(
        pathlib.Path(r"C:\Users\x\Documents\notes.txt"),
        allowed_fragments=frozenset({"riot games"}),
    )


def test_only_launcher_categories_waive_a_path_fragment() -> None:
    """No category picks up a waiver by accident."""
    waiving = {c.id for c in CATEGORIES if c.allowed_path_fragments}
    assert waiving == {
        "cache.launcher.battlenet",
        "cache.launcher.riot",
        "cache.launcher.ea",
        "cache.launcher.ubisoft",
    }


# -- publisher folders and anti-cheat --------------------------------------


def test_every_protected_fragment_is_lowercase() -> None:
    """Names are compared lowercased, so a capital would never match.

    ``"Riot Vanguard"`` in the table would look right and protect nothing.
    """
    from pudge_gaming_manager.windows.cleanup.rules import PROTECTED_PATH_FRAGMENTS

    assert all(f == f.lower() for f in PROTECTED_PATH_FRAGMENTS)


@pytest.mark.parametrize(
    "path",
    [
        # Outside Program Files, where no protected root already covers the
        # path and the name table is the only guard. A ".dat" so the
        # executable-extension guard does not decide it either.
        r"D:\Riot Vanguard\log\a.dat",   # begins with "riot", not "vanguard"
        r"D:\Games\EA Games\Apex\a.dat",
        r"D:\Origin Games\Titanfall\a.dat",
        r"D:\Ubisoft\Ubisoft Game Launcher\games\X\a.dat",
        r"D:\Rockstar Games\GTA V\a.dat",
        r"D:\Wargaming.net\WorldOfTanks\a.dat",
        r"D:\Valve\Steam\a.dat",
        r"C:\XboxGames\Minecraft\Content\a.dat",
        r"D:\EasyAntiCheat_EOS\a.dat",
    ],
)
def test_a_category_cannot_reach_a_publisher_or_anti_cheat_folder(path: str) -> None:
    """The whole table is name-prefix matching, so each folder is named."""
    assert CleanupEngine(()).is_protected(pathlib.Path(path))


def test_the_launcher_waiver_does_not_open_the_publishers_games() -> None:
    """EA's cache category may enter ``Electronic Arts\\EA Desktop\\cache``,
    and nothing else under that publisher or any other."""
    engine = CleanupEngine(())
    ea = frozenset({"electronic arts"})
    cache = pathlib.Path(r"C:\Users\x\AppData\Local\Electronic Arts\EA Desktop\cache\a.tmp")
    assert not engine.is_protected(cache, allowed_fragments=ea)
    # Another vendor's guard is untouched by that waiver.
    vanguard = pathlib.Path(r"D:\Riot Vanguard\log\a.dat")
    assert engine.is_protected(vanguard, allowed_fragments=ea)
    ubisoft = pathlib.Path(r"D:\Ubisoft\games\a.dat")
    assert engine.is_protected(ubisoft, allowed_fragments=ea)


# -- formatting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 Б"), (512, "512 Б"), (2048, "2.0 КБ"), (5 * 1024**3, "5.0 ГБ")],
)
def test_size_formatting(value: int, expected: str) -> None:
    assert format_size(value) == expected


# -- delete by object, not by name (the TOCTOU defence) --------------------


@windows_only
def test_delete_file_removes_a_real_file_and_returns_its_size(
    tmp_path: pathlib.Path,
) -> None:
    from pudge_gaming_manager.utilities.secure_delete import delete_file

    target = _write(tmp_path / "a.tmp", "x" * 321)
    assert delete_file(target) == 321
    assert not target.exists()


@windows_only
def test_delete_file_refuses_a_directory(tmp_path: pathlib.Path) -> None:
    from pudge_gaming_manager.utilities.secure_delete import (
        DeleteError,
        delete_file,
    )

    folder = tmp_path / "sub"
    folder.mkdir()
    with pytest.raises(DeleteError):
        delete_file(folder)
    assert folder.is_dir()


@windows_only
def test_delete_file_refuses_a_junction_swapped_in_after_the_scan(
    tmp_path: pathlib.Path,
) -> None:
    """The core attack: a file the scan approved becomes a junction to a
    protected tree before deletion. Deleting by name would follow it; the
    handle-based delete opens the reparse point as itself and refuses."""
    from pudge_gaming_manager.utilities.secure_delete import (
        DeleteError,
        delete_file,
    )

    victim = tmp_path / "system"
    _write(victim / "kernel.tmp")
    swapped = tmp_path / "a.tmp"
    # Replace the approved file's *name* with a junction to another tree,
    # exactly as an attacker would between scan and clean.
    import subprocess

    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(swapped), str(victim)],
        check=True, capture_output=True,
    )
    with pytest.raises(DeleteError):
        delete_file(swapped)
    assert (victim / "kernel.tmp").exists()


@windows_only
def test_clean_refuses_a_file_replaced_by_a_junction(
    tmp_path: pathlib.Path,
) -> None:
    """End to end: scan lists a file, an attacker swaps it for a junction to
    a protected tree, clean must reclaim nothing from behind the junction."""
    import subprocess

    sandbox = tmp_path / "temp"
    sandbox.mkdir()
    protected = tmp_path / "windows"
    _write(protected / "important.tmp")
    approved = _write(sandbox / "a.tmp")

    engine = CleanupEngine((_category(sandbox),))
    report = engine.scan()
    assert report.total_files == 1

    approved.unlink()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(approved), str(protected)],
        check=True, capture_output=True,
    )

    result = engine.clean(report)
    assert result.deleted_files == 0
    assert result.refused_files == 1
    assert (protected / "important.tmp").exists()


@windows_only
def test_delete_tree_does_not_follow_a_nested_junction(
    tmp_path: pathlib.Path,
) -> None:
    """os.walk(followlinks=False) still descends into NTFS junctions; the
    hand-written walk must not, or a junction planted inside a tree would
    let an elevated delete destroy its target's contents."""
    from pudge_gaming_manager.utilities.secure_delete import delete_tree
    import subprocess

    victim = tmp_path / "system"
    _write(victim / "kernel.dll")
    tree = tmp_path / "workshop" / "content"
    tree.mkdir(parents=True)
    _write(tree / "real.dat")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tree / "planted"), str(victim)],
        check=True, capture_output=True,
    )

    delete_tree(tmp_path / "workshop")
    assert not (tmp_path / "workshop").exists()  # our tree is gone
    assert (victim / "kernel.dll").exists()  # the junction target is not


@windows_only
def test_delete_tree_clears_a_read_only_file(tmp_path: pathlib.Path) -> None:
    """Steam marks some game files read-only; like -Force, they must go."""
    import os
    import stat as _stat
    from pudge_gaming_manager.utilities.secure_delete import delete_tree

    tree = tmp_path / "game"
    _write(tree / "locked.bin", "data")
    os.chmod(tree / "locked.bin", _stat.S_IREAD)
    delete_tree(tree)
    assert not tree.exists()



@windows_only
def test_links_in_temp_are_neither_candidates_nor_refusals(tmp_path: pathlib.Path) -> None:
    """pytest leaves ``...current`` directory links in Temp. Yielding them
    as candidates made every cleanup report hundreds of "refused - possible
    swap" items on a machine where nothing was wrong."""
    import subprocess

    sandbox = tmp_path / "sandbox"
    target = tmp_path / "target"
    _write(sandbox / "real.tmp")
    _write(target / "keep.dat")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(sandbox / "run-current"), str(target)],
        check=True, capture_output=True,
    )

    engine = CleanupEngine((_category(sandbox),))
    report = engine.scan()
    names = [i.path.name for i in report.categories[0].items]
    result = engine.clean(report)

    assert names == ["real.tmp"]
    assert result.refused_files == 0
    assert (target / "keep.dat").exists()


@windows_only
def test_empty_folder_pruning_does_not_walk_into_a_junction(tmp_path: pathlib.Path) -> None:
    import subprocess

    sandbox = tmp_path / "sandbox"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "empty-but-not-ours").mkdir(parents=True)
    _write(sandbox / "nested" / "old.tmp")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(sandbox / "link"), str(elsewhere)],
        check=True, capture_output=True,
    )

    engine = CleanupEngine((_category(sandbox),))
    engine.clean(engine.scan())

    assert not (sandbox / "nested").exists()  # emptied by the clean, then pruned
    assert (sandbox / "link").exists()  # a link is not an empty folder of ours
    assert (elsewhere / "empty-but-not-ours").exists()
