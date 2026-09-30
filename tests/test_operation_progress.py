"""Progress from disk cleanup, the Steam reset and the usage survey.

What is tested is what the operator sees through it: the order of the steps,
that the bar only moves forward and ends full, that a dry run says nothing,
and that a report which fails cannot stop the work. Everything runs on
throwaway trees; the Recycle Bin and the commands that stop Steam are faked.
"""

from __future__ import annotations

import os
import pathlib
from types import SimpleNamespace

import pytest

from pudge_gaming_manager.core.cleanup import DiskCleaner
from pudge_gaming_manager.games.steam import wipe as wipe_module
from pudge_gaming_manager.games.steam.models import SteamInstall
from pudge_gaming_manager.games.steam.wipe import SteamWiper
from pudge_gaming_manager.windows import usage
from pudge_gaming_manager.windows.cleanup import engine as engine_module
from pudge_gaming_manager.windows.cleanup import recycle_bin
from pudge_gaming_manager.windows.cleanup.engine import CleanupEngine
from pudge_gaming_manager.windows.cleanup.recycle_bin import RecycleBinState
from pudge_gaming_manager.windows.cleanup.rules import CleanupCategory, CleanupRisk


def _always_ready(*_a, **_k):
    """A throttle that lets every report through, so each step is visible."""
    return SimpleNamespace(ready=lambda: True)


def _forward_and_full(events) -> None:
    fractions = [e.fraction for e in events if e.fraction is not None]
    assert fractions == sorted(fractions), "the bar went backwards"
    assert fractions and fractions[-1] == 1.0, "the bar did not end full"


# -- disk cleanup ---------------------------------------------------------------


def _category(root: pathlib.Path, **kwargs) -> CleanupCategory:
    os.environ["PGM_TEST_ROOT"] = str(root)
    fields = {
        "id": "test.category", "name": "Тестовая категория",
        "description": "Files made by the test suite.",
        "rationale": "Lives only in a pytest temporary directory.",
        "roots": ("%PGM_TEST_ROOT%",), "min_age_hours": 0.0, "risk": CleanupRisk.SAFE,
    }
    fields.update(kwargs)
    return CleanupCategory(**fields)  # type: ignore[arg-type]


def _junk(root: pathlib.Path, count: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"junk{index}.tmp").write_bytes(b"x" * 100)


def test_cleanup_names_the_category_and_ends_full(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "Throttle", _always_ready)
    _junk(tmp_path, 5)
    engine = CleanupEngine((_category(tmp_path),))
    events: list = []

    result = engine.clean(engine.scan(), progress=events.append)

    assert result.deleted_files == 5
    assert "Тестовая категория" in events[0].text
    assert any("удалено" in e.text for e in events)
    _forward_and_full(events)


def test_a_dry_run_reports_nothing(tmp_path) -> None:
    _junk(tmp_path, 3)
    engine = CleanupEngine((_category(tmp_path),))
    events: list = []

    engine.clean(engine.scan(), dry_run=True, progress=events.append)

    assert events == []


def test_pruning_empty_folders_is_announced_as_a_step(tmp_path) -> None:
    _junk(tmp_path / "a" / "b", 2)
    engine = CleanupEngine((_category(tmp_path, remove_empty_dirs=True),))
    events: list = []

    engine.clean(engine.scan(), progress=events.append)

    assert any("пустых папок" in e.text for e in events)


def test_a_broken_progress_callback_does_not_stop_the_cleanup(tmp_path) -> None:
    _junk(tmp_path, 4)
    engine = CleanupEngine((_category(tmp_path),))

    def broken(_event) -> None:
        raise RuntimeError("the window is gone")

    result = engine.clean(engine.scan(), progress=broken)

    assert result.deleted_files == 4
    assert not list(tmp_path.glob("*.tmp"))


def test_emptying_the_recycle_bin_is_a_step_without_a_number(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        recycle_bin, "query", lambda: RecycleBinState(size_bytes=2048, item_count=3)
    )
    monkeypatch.setattr(recycle_bin, "empty", lambda: (True, "ok"))
    cleaner = DiskCleaner((_category(tmp_path),))
    events: list = []

    cleaner.run(cleaner.plan(), {"recycle_bin"}, progress=events.append)

    bin_steps = [e for e in events if "корзины" in e.text]
    assert bin_steps and bin_steps[0].fraction is None  # busy: nothing to measure


# -- Steam ------------------------------------------------------------------------


def _steam(root: pathlib.Path, games: dict[int, tuple[str, int]]) -> SteamInstall:
    apps = root / "steamapps"
    (apps / "common").mkdir(parents=True)
    escaped = str(root).replace("\\", "\\\\")
    (apps / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n  "0"\n  {{\n    "path"  "{escaped}"\n  }}\n}}\n',
        encoding="utf-8",
    )
    for app_id, (name, size) in games.items():
        (apps / f"appmanifest_{app_id}.acf").write_text(
            '"AppState"\n{\n'
            f'    "appid"   "{app_id}"\n    "name"    "{name}"\n'
            f'    "installdir"  "{name.lower()}"\n    "SizeOnDisk"  "{size}"\n}}\n',
            encoding="utf-8",
        )
        game = apps / "common" / name.lower()
        game.mkdir()
        (game / "game.bin").write_bytes(b"x" * size)
    return SteamInstall(path=root)


@pytest.fixture()
def wiper(monkeypatch) -> SteamWiper:
    monkeypatch.setattr(wipe_module, "find_steam", lambda: None)
    instance = SteamWiper()
    instance._stop_steam = lambda: True  # never stop a real Steam in a test
    return instance


def _scan(monkeypatch, wiper: SteamWiper, root: pathlib.Path):
    monkeypatch.setattr(wipe_module, "find_steam", lambda: SteamInstall(path=root))
    return wiper.scan(keep_app_ids=set())


def test_the_steam_reset_walks_through_its_steps(tmp_path, monkeypatch, wiper) -> None:
    _steam(tmp_path, {1: ("Alpha", 4000), 2: ("Beta", 6000)})
    plan = _scan(monkeypatch, wiper, tmp_path)
    events: list = []

    wiper.wipe(plan, progress=events.append)

    texts = [e.text for e in events]
    assert "остановка Steam" in texts[0]
    assert any("игра 1 из 2" in t for t in texts)
    assert any("игра 2 из 2" in t for t in texts)
    assert texts.index(next(t for t in texts if "игра 2 из 2" in t)) > texts.index(
        next(t for t in texts if "игра 1 из 2" in t)
    )


def test_the_steam_bar_counts_bytes_and_never_goes_back(tmp_path, monkeypatch, wiper) -> None:
    _steam(tmp_path, {1: ("Alpha", 4000), 2: ("Beta", 6000)})
    plan = _scan(monkeypatch, wiper, tmp_path)
    events: list = []

    wiper.wipe(plan, progress=events.append)

    fractions = [e.fraction for e in events if e.fraction is not None]
    assert fractions == sorted(fractions)
    assert 0.0 <= fractions[0] and fractions[-1] <= 1.0
    # A 6000-byte game moves the bar further than a 4000-byte one.
    alpha_done = next(e.fraction for e in events if "игра 2 из 2" in e.text)
    assert 0.3 < alpha_done < 0.5  # 4000 of 10000 bytes, not 1 of 2 games


def test_a_steam_dry_run_reports_nothing(tmp_path, monkeypatch, wiper) -> None:
    _steam(tmp_path, {1: ("Alpha", 4000)})
    plan = _scan(monkeypatch, wiper, tmp_path)
    events: list = []

    wiper.wipe(plan, dry_run=True, progress=events.append)

    assert events == []
    assert (tmp_path / "steamapps" / "common" / "alpha").exists()


def test_a_broken_progress_callback_does_not_stop_the_steam_reset(
    tmp_path, monkeypatch, wiper
) -> None:
    _steam(tmp_path, {1: ("Alpha", 4000)})
    plan = _scan(monkeypatch, wiper, tmp_path)

    def broken(_event) -> None:
        raise RuntimeError("the window is gone")

    result = wiper.wipe(plan, progress=broken)

    assert result.removed_games == 1
    assert not (tmp_path / "steamapps" / "common" / "alpha").exists()


# -- the usage survey ----------------------------------------------------------------


def test_the_survey_reports_each_root_with_a_rising_bar(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(usage, "Throttle", _always_ready)
    roots = []
    for name in ("one", "two"):
        (tmp_path / name / "x" / "y").mkdir(parents=True)
        (tmp_path / name / "x" / "y" / "f.bin").write_bytes(b"x" * 100)
        roots.append(str(tmp_path / name))
    events: list = []

    usage.survey(tuple(roots), depth=1, progress=events.append)

    assert any("1 из 2" in e.text for e in events)
    assert any("2 из 2" in e.text for e in events)
    fractions = [e.fraction for e in events]
    assert fractions == sorted(fractions) and fractions[-1] < 1.0  # the survey's own end closes it
