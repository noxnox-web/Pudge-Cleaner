"""Which tweaks an optimization pass offers, and why.

``build_tweaks`` is the selection step: it decides what the operator will be
shown before the engine applies any safety gate. These tests pin the two
changes that were made to it — cleanup is no longer conditional on disk
pressure, and the power-plan tweak is actually reachable.
"""

from __future__ import annotations

import pytest

from pudge_gaming_manager.core.optimization.pipeline import (
    OptimizationLevel,
    OptimizationPipeline,
)
from pudge_gaming_manager.core.optimization.tweak import RiskLevel
from pudge_gaming_manager.core.optimization.tweaks.cleanup import (
    CleanTemporaryFilesTweak,
)
from pudge_gaming_manager.core.optimization.tweaks.power import SetPowerPlanTweak
from pudge_gaming_manager.core.optimization.tweaks.recycle_bin import (
    EmptyRecycleBinTweak,
)
from pudge_gaming_manager.core.optimization.tweaks.windows_old import (
    RemoveWindowsOldTweak,
)
from pudge_gaming_manager.hardware.models import (
    CpuInfo,
    DiskInfo,
    HardwareSnapshot,
    OsInfo,
    RamInfo,
    Reading,
)


def _snapshot(**kwargs) -> HardwareSnapshot:
    base = {
        "captured_at": "2026-09-24T00:00:00+00:00",
        "os": OsInfo(),
        "cpu": CpuInfo(),
        "gpus": (),
        "ram": RamInfo(),
        "disks": (),
        "monitors": (),
    }
    base.update(kwargs)
    return HardwareSnapshot(**base)  # type: ignore[arg-type]


def _roomy_disk() -> DiskInfo:
    """A system disk with plenty of space — the case that used to skip cleanup."""
    return DiskInfo(
        device_id="C:",
        total_gb=Reading(value=1000.0, unit="GB", source="test"),
        free_gb=Reading(value=900.0, unit="GB", source="test"),
        free_percent=Reading(value=90.0, unit="%", source="test"),
        is_system_disk=True,
    )


@pytest.fixture()
def pipeline(tmp_path) -> OptimizationPipeline:
    from pudge_gaming_manager.database.connection import Database

    return OptimizationPipeline(Database(tmp_path / "test.db"))


def _ids(tweaks) -> set[str]:
    return {t.id for t in tweaks}


def test_cleanup_is_offered_even_with_a_nearly_empty_disk(
    pipeline: OptimizationPipeline,
) -> None:
    """The old gate skipped cleanup entirely above 25% free.

    That made shader caches and crash dumps unreachable on any healthy PC,
    which reads to an operator as "the program does not clean anything".
    """
    tweaks = pipeline.build_tweaks(_snapshot(disks=(_roomy_disk(),)))

    assert CleanTemporaryFilesTweak.id in _ids(tweaks)


def test_cleanup_is_offered_with_no_disk_information_at_all(
    pipeline: OptimizationPipeline,
) -> None:
    """A disk the scan could not read is not a reason to skip cleanup."""
    tweaks = pipeline.build_tweaks(_snapshot())

    assert CleanTemporaryFilesTweak.id in _ids(tweaks)


def test_the_power_plan_tweak_is_reachable(
    pipeline: OptimizationPipeline,
) -> None:
    """It was written and tested but never added to a plan, so it never ran."""
    tweaks = pipeline.build_tweaks(_snapshot(disks=(_roomy_disk(),)))

    assert SetPowerPlanTweak.id in _ids(tweaks)


def test_windows_old_removal_stays_in_the_plan(
    pipeline: OptimizationPipeline,
) -> None:
    tweaks = pipeline.build_tweaks(_snapshot(disks=(_roomy_disk(),)))

    assert RemoveWindowsOldTweak.id in _ids(tweaks)


def test_no_tweak_is_offered_twice(pipeline: OptimizationPipeline) -> None:
    """The engine registers tweaks by id; a duplicate would shadow itself."""
    tweaks = pipeline.build_tweaks(_snapshot(disks=(_roomy_disk(),)))

    ids = [t.id for t in tweaks]
    assert len(ids) == len(set(ids))


def test_the_cleanup_tweak_stays_auto_applicable(
    pipeline: OptimizationPipeline,
) -> None:
    """Its risk is the highest of its categories, and the engine gates on that.

    Browser caches joined the defaults; had they been classed MEDIUM the
    whole cleanup tweak would have inherited MEDIUM and been refused by a
    pipeline running with ``allow_risk_above_low=False`` — cleanup would
    have silently stopped working.
    """
    cleanup = next(
        t
        for t in pipeline.build_tweaks(_snapshot())
        if t.id == CleanTemporaryFilesTweak.id
    )

    assert cleanup.risk.auto_applicable


def test_the_recycle_bin_is_offered_by_optimize(
    pipeline: OptimizationPipeline,
) -> None:
    """It was only reachable from the cleanup button, which was inconsistent.

    OPTIMIZE PC reclaims disk space; the bin is disk space. Leaving it out
    meant two buttons that both say "free up space" disagreed about what
    that includes.
    """
    tweaks = pipeline.build_tweaks(_snapshot(disks=(_roomy_disk(),)))

    assert EmptyRecycleBinTweak.id in _ids(tweaks)


def test_a_medium_tweak_is_offered_but_held_back(
    pipeline: OptimizationPipeline,
) -> None:
    """MEDIUM used to be unreachable; now it is reachable but not automatic.

    Before the preview grew its opt-in, a MEDIUM tweak was dropped by the
    gate before the operator saw the row — present in the code, absent from
    the product. It must now appear, marked as held back for its risk, and
    only that: still not applied, and not confused with a change that was
    refused for some other reason.
    """
    plan = pipeline.preview(_snapshot(disks=(_roomy_disk(),))).plan
    medium = [
        c for c in plan.changes if not c.tweak.risk.auto_applicable
    ]
    assert medium, "the catalogue no longer contains a MEDIUM tweak"

    for change in medium:
        assert not change.will_apply, change.tweak.id
        # Either it needs nothing doing, or the risk gate is the one thing
        # holding it. Nothing MEDIUM may be applied without the opt-in.
        assert change.blocked_by_risk or not change.state.needs_change


def test_the_opt_in_makes_medium_applicable(
    pipeline: OptimizationPipeline,
) -> None:
    held = pipeline.preview(_snapshot(disks=(_roomy_disk(),)))
    if not held.plan.blocked_by_risk:
        pytest.skip("nothing MEDIUM needs changing on this machine")

    opened = pipeline.preview(
        _snapshot(disks=(_roomy_disk(),)), level=OptimizationLevel.MEDIUM
    )

    assert not opened.plan.blocked_by_risk
    assert opened.change_count > held.change_count
    assert opened.allow_risk_above_low


def test_the_opt_in_never_reaches_critical(
    pipeline: OptimizationPipeline,
) -> None:
    """CRITICAL is defined as "could leave the machine unusable"."""
    for change in pipeline.preview(
        _snapshot(disks=(_roomy_disk(),)), level=OptimizationLevel.MEDIUM
    ).plan.changes:
        assert change.tweak.risk is not RiskLevel.CRITICAL


def test_an_empty_recycle_bin_reports_no_change_rather_than_vanishing(
    pipeline: OptimizationPipeline, monkeypatch
) -> None:
    from pudge_gaming_manager.core.optimization.tweak import TweakContext
    from pudge_gaming_manager.windows.cleanup import recycle_bin
    from pudge_gaming_manager.windows.cleanup.recycle_bin import RecycleBinState

    monkeypatch.setattr(recycle_bin, "query", lambda: RecycleBinState())
    state = EmptyRecycleBinTweak().scan(TweakContext())

    assert not state.needs_change
    assert "пуста" in state.summary


def test_an_unreadable_recycle_bin_is_refused_not_guessed(monkeypatch) -> None:
    from pudge_gaming_manager.core.optimization.tweak import TweakContext
    from pudge_gaming_manager.windows.cleanup import recycle_bin
    from pudge_gaming_manager.windows.cleanup.recycle_bin import RecycleBinState

    monkeypatch.setattr(
        recycle_bin,
        "query",
        lambda: RecycleBinState(available=False, unavailable_reason="нет доступа"),
    )
    tweak = EmptyRecycleBinTweak()
    ctx = TweakContext()
    state = tweak.scan(ctx)

    assert not state.needs_change
    assert not tweak.validate(ctx, state).ok
