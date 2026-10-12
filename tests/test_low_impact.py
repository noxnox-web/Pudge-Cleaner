"""Staying out of the player's way: priority, idleness, the window marker."""

from __future__ import annotations

import os

import psutil
import pytest

from pudge_gaming_manager.utilities import low_impact, single_instance


def test_idle_time_is_a_non_negative_number_or_unknown() -> None:
    idle = low_impact.idle_seconds()
    assert idle is None or idle >= 0


def test_priority_is_lowered_for_cpu_and_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    class FakeProcess:
        def nice(self, value):
            calls.append(("nice", value))

        def ionice(self, value):
            calls.append(("ionice", value))

    monkeypatch.setattr(low_impact.psutil, "Process", FakeProcess)
    low_impact.lower_priority()
    assert calls == [
        ("nice", psutil.IDLE_PRIORITY_CLASS),
        ("ionice", psutil.IOPRIO_VERYLOW),
    ]


def test_refused_priority_change_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idle wait already protects the player; the run must still go on."""
    attempted: list = []

    class Refusing:
        def nice(self, value):
            attempted.append("nice")
            raise psutil.AccessDenied()

        def ionice(self, value):
            attempted.append("ionice")

    monkeypatch.setattr(low_impact.psutil, "Process", Refusing)
    low_impact.lower_priority()
    assert attempted == ["nice", "ionice"]  # one refusal does not skip the other


@pytest.mark.skipif(os.name != "nt", reason="named mutexes are a Windows facility")
def test_a_held_mutex_is_seen_until_released() -> None:
    name = f"Local\\PudgeCleaner.Test.{os.getpid()}"
    assert not single_instance.is_held(name)
    assert single_instance.hold(name)
    try:
        assert single_instance.is_held(name)
    finally:
        single_instance.release(name)
    assert not single_instance.is_held(name)


def test_the_window_marker_is_in_the_global_namespace() -> None:
    """The nightly task and the window may run in different sessions."""
    assert single_instance.GUI_MUTEX.startswith("Global\\")
