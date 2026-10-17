"""Tests for the «Ядра GPU» backend.

Nothing here touches the real registry, restarts a real driver or starts a
real program (rule #48): the registry is an in-memory fake, the tweak runs
against a dict of policies, and the benchmark runs against a fake process
runner that writes PresentMon-shaped CSVs.
"""

from __future__ import annotations

import json
import pathlib
import struct

import pytest

from pudge_gaming_manager.core.benchmark import frametime
from pudge_gaming_manager.core.gpu_affinity import selection
from pudge_gaming_manager.core.gpu_affinity.scan import (
    AffinityScan,
    ScanSettings,
    estimate_seconds,
)
from pudge_gaming_manager.core.gpu_affinity.selection import CoreResult
from pudge_gaming_manager.core.gpu_affinity.service import GpuAffinityService
from pudge_gaming_manager.core.gpu_affinity.tools import BenchTools
from pudge_gaming_manager.core.optimization import history
from pudge_gaming_manager.core.optimization.tweak import Outcome
from pudge_gaming_manager.core.optimization.tweaks.gpu_affinity import (
    GpuInterruptAffinityTweak,
)
from pudge_gaming_manager.database.connection import Database
from pudge_gaming_manager.hardware.cpu import topology
from pudge_gaming_manager.hardware.cpu.topology import CpuCore
from pudge_gaming_manager.utilities.exceptions import PgmError
from pudge_gaming_manager.windows.devices import affinity, msi
from pudge_gaming_manager.windows.devices.affinity import AffinityError, AffinityPolicy

GPU = r"PCI\VEN_10DE&DEV_2504&SUBSYS_00000000&REV_A1\4&1234&0&0008"
ADAPTERS = [("NVIDIA GeForce RTX 3060", GPU)]


# -- topology -------------------------------------------------------------------


def _record(logical: int, core: int, llc: int = 0, efficiency: int = 0) -> bytes:
    body = struct.pack("<IIIHBBBBBB", 32, 0, 0, 0, logical, core, llc, 0, efficiency, 0)
    return body + bytes(32 - len(body))


def _smt(cores: int) -> list[CpuCore]:
    buffer = b"".join(
        _record(lp, lp // 2) for lp in range(cores * 2)
    )
    return topology.parse_cores(buffer)


def test_cores_group_a_cores_threads_together() -> None:
    cores = _smt(3)

    assert [c.logical for c in cores] == [(0, 1), (2, 3), (4, 5)]
    assert [c.primary for c in cores] == [0, 2, 4]


def test_a_core_remembers_its_efficiency_class() -> None:
    buffer = _record(0, 0, efficiency=1) + _record(1, 1, efficiency=0)

    assert [c.efficiency_class for c in topology.parse_cores(buffer)] == [1, 0]


# -- which cores to test ----------------------------------------------------------


def test_candidates_are_the_first_thread_of_each_core_except_the_main_one() -> None:
    assert selection.candidate_cpus(_smt(6)) == [2, 4, 6, 8, 10]


def test_without_smt_every_core_but_cpu0_is_a_candidate() -> None:
    cores = topology.parse_cores(b"".join(_record(n, n) for n in range(4)))

    assert selection.candidate_cpus(cores) == [1, 2, 3]


def test_a_hybrid_cpu_tries_only_its_fastest_cores() -> None:
    buffer = b"".join(
        [_record(0, 0, efficiency=1), _record(1, 0, efficiency=1),
         _record(2, 1, efficiency=1), _record(3, 1, efficiency=1),
         _record(4, 2, efficiency=0), _record(5, 3, efficiency=0)]
    )

    assert selection.candidate_cpus(topology.parse_cores(buffer)) == [2]


def test_the_main_core_is_the_one_holding_cpu0() -> None:
    main = selection.main_core(_smt(4))

    assert main is not None and main.logical == (0, 1)


# -- ranking -------------------------------------------------------------------------


def _result(cpu: int, average: float, low_1: float, low_01: float) -> CoreResult:
    return CoreResult(
        cpu, frametime.FrameStats(1000, average, low_01, average, 1.0, low_1, low_01)
    )


def test_a_core_that_wins_everything_is_best() -> None:
    results = [_result(2, 900, 700, 500), _result(4, 1000, 800, 600), _result(6, 800, 600, 400)]

    assert selection.best(results).cpu == 4
    assert [r.cpu for r in selection.ranked(results)] == [4, 2, 6]


def test_a_stuttering_core_does_not_win_on_average_alone() -> None:
    steady = _result(4, 950, 900, 880)
    spiky = _result(6, 1000, 500, 200)
    third = _result(8, 900, 850, 800)

    assert selection.best([spiky, steady, third]).cpu == 4


def test_ties_fall_back_to_the_average() -> None:
    a = _result(2, 1000, 500, 500)
    b = _result(4, 900, 600, 600)
    c = _result(6, 1100, 400, 400)

    # Ranks: a=1+1+... all different measures disagree; the sum ties, the average decides.
    assert selection.best([a, b, c]).cpu in {2, 4, 6}
    assert selection.ranked([a, b, c])[0].stats.average >= 900


def test_spread_is_best_over_worst() -> None:
    assert selection.spread_percent([_result(2, 1000, 1, 1), _result(4, 1100, 1, 1)]) == pytest.approx(10)
    assert selection.spread_percent([_result(2, 1000, 1, 1)]) == 0


# -- frame statistics ------------------------------------------------------------------


def test_a_steady_capture_has_equal_average_and_lows() -> None:
    result = frametime.stats([2.0] * 500)

    assert result is not None
    assert result.average == pytest.approx(500)
    assert result.low_1 == pytest.approx(500) and result.low_01 == pytest.approx(500)
    assert result.stdev == pytest.approx(0)


def test_stutter_pulls_the_lows_down_but_not_the_average_much() -> None:
    times = [2.0] * 990 + [20.0] * 10

    result = frametime.stats(times)

    assert result is not None
    assert result.low_1 == pytest.approx(50)
    assert result.average > 300


def test_too_few_frames_say_nothing() -> None:
    assert frametime.stats([2.0] * (frametime.MIN_FRAMES - 1)) is None


def test_presentmon_csv_is_read_whatever_the_header_case(tmp_path: pathlib.Path) -> None:
    for header in ("msBetweenPresents", "MsBetweenPresents"):
        path = tmp_path / f"{header}.csv"
        path.write_text(f"Application,{header}\nx.exe,2.5\nx.exe,bad\nx.exe,3.5\n", encoding="utf-8")

        assert frametime.read_presentmon_csv(path) == [2.5, 3.5]


# -- the policy ------------------------------------------------------------------------


def test_pinning_names_one_core_with_high_priority() -> None:
    policy = AffinityPolicy.pinned(6)

    assert policy.policy == 4 and policy.priority == 3
    assert policy.mask == bytes([0x40]) and policy.cpus == (6,)
    assert "CPU 6" in policy.label


def test_a_high_core_needs_more_mask_bytes() -> None:
    assert AffinityPolicy.pinned(9).mask == bytes([0x00, 0x02])


def test_a_policy_survives_json() -> None:
    for policy in (AffinityPolicy(), AffinityPolicy.pinned(4), AffinityPolicy(True, 0, None, None)):
        assert AffinityPolicy.from_dict(json.loads(json.dumps(policy.to_dict()))) == policy


def test_the_default_policy_reads_as_windows_deciding() -> None:
    assert "Windows" in AffinityPolicy().label


def test_a_core_beyond_the_mask_is_refused() -> None:
    with pytest.raises(AffinityError):
        AffinityPolicy.pinned(64)


@pytest.mark.parametrize(
    "bad",
    ["", "USB\\VID_1&PID_2\\3", "PCI\\..\\..\\x", "PCI\\VEN_1\\a\\b", GPU + "\\Evil", "PCI\\VEN_1 &DEV\\2"],
)
def test_only_pci_instance_ids_reach_the_registry(bad: str) -> None:
    with pytest.raises(AffinityError):
        affinity.check_instance_id(bad)


# -- the registry, faked ----------------------------------------------------------------


class FakeRegistry:
    """Just enough of ``winreg`` for the affinity module."""

    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ, KEY_SET_VALUE, KEY_WOW64_64KEY = 1, 2, 4
    REG_DWORD, REG_BINARY = 4, 3

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, object]] = {}

    def OpenKey(self, _hive, path, _reserved, _access):  # noqa: N802
        if path not in self.keys:
            raise FileNotFoundError(path)
        return _Handle(self.keys[path])

    def CreateKeyEx(self, _hive, path, _reserved, _access):  # noqa: N802
        return _Handle(self.keys.setdefault(path, {}))

    def QueryValueEx(self, handle, name):  # noqa: N802
        if name not in handle.values:
            raise FileNotFoundError(name)
        return handle.values[name], 0

    def SetValueEx(self, handle, name, _reserved, _kind, value):  # noqa: N802
        handle.values[name] = value

    def DeleteValue(self, handle, name):  # noqa: N802
        if name not in handle.values:
            raise FileNotFoundError(name)
        del handle.values[name]

    def DeleteKeyEx(self, _hive, path, _access, _reserved):  # noqa: N802
        if path not in self.keys:
            raise FileNotFoundError(path)
        del self.keys[path]


class _Handle:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    fake = FakeRegistry()
    monkeypatch.setattr(affinity, "winreg", fake)
    return fake


def test_a_device_without_the_key_reads_as_no_policy(registry: FakeRegistry) -> None:
    assert affinity.read(GPU) == AffinityPolicy()


def test_a_written_policy_reads_back(registry: FakeRegistry) -> None:
    affinity.write(GPU, AffinityPolicy.pinned(6))

    assert affinity.read(GPU) == AffinityPolicy.pinned(6)
    (stored,) = registry.keys.values()
    assert stored == {"DevicePolicy": 4, "DevicePriority": 3, "AssignmentSetOverride": bytes([0x40])}


def test_writing_no_policy_removes_the_key(registry: FakeRegistry) -> None:
    affinity.write(GPU, AffinityPolicy.pinned(6))

    affinity.write(GPU, AffinityPolicy())

    assert registry.keys == {}


def test_a_value_the_snapshot_lacked_is_deleted_not_blanked(registry: FakeRegistry) -> None:
    affinity.write(GPU, AffinityPolicy.pinned(6))
    only_policy = AffinityPolicy(key_present=True, policy=0)

    affinity.write(GPU, only_policy)

    assert affinity.read(GPU) == only_policy
    (stored,) = registry.keys.values()
    assert set(stored) == {"DevicePolicy"}


# -- the tweak, through the real engine -------------------------------------------------------


class FakeDevices:
    """A dict standing in for the registry, with the tweak's reader/writer."""

    def __init__(self, initial: dict[str, AffinityPolicy] | None = None) -> None:
        self.policies = dict(initial or {GPU: AffinityPolicy()})
        self.writes: list[tuple[str, AffinityPolicy]] = []

    def read(self, pnp: str) -> AffinityPolicy:
        return self.policies.get(pnp, AffinityPolicy())

    def write(self, pnp: str, policy: AffinityPolicy) -> None:
        self.writes.append((pnp, policy))
        self.policies[pnp] = policy


@pytest.fixture()
def database(tmp_path: pathlib.Path) -> Database:
    return Database(tmp_path / "pgm.db")


def _tweak(devices: FakeDevices, cpu: int = 6) -> GpuInterruptAffinityTweak:
    return GpuInterruptAffinityTweak(ADAPTERS, cpu, devices.read, devices.write)


def _service(database: Database, devices: FakeDevices, **overrides) -> GpuAffinityService:
    options = dict(
        cores=lambda: _smt(6),
        adapters=lambda: ADAPTERS,
        read=devices.read,
        msi_state=lambda _pnp: msi.MsiState(True),
        admin=lambda: True,
        display_ok=lambda: True,
        locate_tools=lambda: BenchTools(pathlib.Path("scene.exe"), pathlib.Path("pm.exe")),
    )
    options.update(overrides)
    return GpuAffinityService(database, **options)


def _apply(service: GpuAffinityService, devices: FakeDevices, cpu: int = 6):
    """Plan with the real engine, but with the fake devices behind the tweak."""
    from pudge_gaming_manager.core.optimization.engine import TweakEngine
    from pudge_gaming_manager.core.optimization.tweak import TweakContext

    engine = TweakEngine(service.database, TweakContext(), allow_risk_above_low=True)
    tweak = _tweak(devices, cpu)
    engine.register([tweak])
    plan = engine.plan([tweak])
    return plan, engine.apply(plan)


def test_applying_pins_the_gpu_and_verifies_by_reading_back(database: Database) -> None:
    devices = FakeDevices()

    _plan, report = _apply(_service(database, devices), devices)

    assert report.results[0].outcome is Outcome.SUCCESS
    assert devices.policies[GPU] == AffinityPolicy.pinned(6)
    assert report.needs_restart


def test_applying_the_same_core_again_changes_nothing(database: Database) -> None:
    devices = FakeDevices({GPU: AffinityPolicy.pinned(6)})

    plan, report = _apply(_service(database, devices), devices)

    assert not plan.applicable
    assert devices.writes == []
    assert report.results[0].outcome is Outcome.NOT_NEEDED


def test_a_policy_the_registry_refuses_to_keep_is_rolled_back(database: Database) -> None:
    devices = FakeDevices()
    original_write = devices.write
    devices.write = lambda pnp, policy: original_write(pnp, AffinityPolicy())  # silently ignored

    _plan, report = _apply(_service(database, devices), devices)

    assert report.results[0].outcome is not Outcome.SUCCESS
    assert devices.policies[GPU] == AffinityPolicy()


def test_history_undoes_a_pin_back_to_no_policy(database: Database) -> None:
    devices = FakeDevices()
    _apply(_service(database, devices), devices)
    (record,) = [c for c in history.list_changes(database) if c.is_applied]

    result = history.restore(database, record.backup_id, resolve=lambda _r: _tweak(devices))

    assert result.ok, result.message
    assert devices.policies[GPU] == AffinityPolicy()


def test_history_restores_a_previously_chosen_core(database: Database) -> None:
    devices = FakeDevices({GPU: AffinityPolicy.pinned(2)})
    _apply(_service(database, devices), devices, cpu=6)
    (record,) = [c for c in history.list_changes(database) if c.is_applied]

    history.restore(database, record.backup_id, resolve=lambda _r: _tweak(devices))

    assert devices.policies[GPU] == AffinityPolicy.pinned(2)


def test_the_tweak_is_known_to_history_without_arguments() -> None:
    from pudge_gaming_manager.core.optimization.history import _FACTORIES

    assert isinstance(_FACTORIES[GpuInterruptAffinityTweak.id](None), GpuInterruptAffinityTweak)


# -- the service ---------------------------------------------------------------------------------


def test_overview_lists_adapters_candidates_and_the_main_core(database: Database) -> None:
    overview = _service(database, FakeDevices()).overview()

    assert [a.name for a in overview.adapters] == ["NVIDIA GeForce RTX 3060"]
    assert overview.adapters[0].msi_on is True
    assert overview.candidates == (2, 4, 6, 8, 10)
    assert "CPU 0–1" in overview.main_core_note
    assert overview.blockers == ()


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"admin": lambda: False}, "администратора"),
        ({"adapters": lambda: []}, "видеокарты"),
        ({"cores": lambda: []}, "процессора"),
        ({"cores": lambda: _smt(2)}, "два ядра"),
        ({"display_ok": lambda: False}, "Базовый видеоадаптер"),
    ],
)
def test_overview_says_why_the_benchmark_cannot_run(database: Database, override, expected: str) -> None:
    overview = _service(database, FakeDevices(), **override).overview()

    assert any(expected in b for b in overview.blockers)


def test_a_missing_tool_blocks_the_benchmark(database: Database) -> None:
    def missing():
        raise PgmError("Не найден тестовый компонент «lava-triangle.exe»", "нет файла")

    overview = _service(database, FakeDevices(), locate_tools=missing).overview()

    assert any("lava-triangle" in b for b in overview.blockers)


def test_restarting_reports_the_adapter_that_would_not_come_back(database: Database) -> None:
    def restart(pnp: str) -> None:
        raise PgmError("не вышло", "драйвер не отвечает")

    service = _service(database, FakeDevices(), restart=restart)

    problems = service.restart_adapters(ADAPTERS, lambda _p: None)

    assert problems == ["NVIDIA GeForce RTX 3060: драйвер не отвечает"]


# -- the benchmark ----------------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, pid: int, *, runs_forever: bool) -> None:
        self.pid = pid
        self._runs_forever = runs_forever
        self.stopped = False

    @property
    def running(self) -> bool:
        return self._runs_forever and not self.stopped

    def stop(self, timeout_s: float = 5.0) -> None:
        self.stopped = True


class FakeRunner:
    """Starts the 'scene' and 'PresentMon'; PresentMon writes a CSV at once."""

    def __init__(self, devices: FakeDevices, fps_by_cpu: dict[int, float], frames: int = 400) -> None:
        self.devices = devices
        self.fps_by_cpu = fps_by_cpu
        self.frames = frames
        self.scene_fails = False
        self.spawned: list[list[str]] = []
        self.scenes: list[FakeProcess] = []
        self.pins_seen: list[int] = []

    def spawn(self, args, *, cwd=None):
        argv = [str(a) for a in args]
        self.spawned.append(argv)
        if argv[0].endswith("scene.exe"):
            scene = FakeProcess(1000 + len(self.scenes), runs_forever=not self.scene_fails)
            self.scenes.append(scene)
            return scene
        # PresentMon: the pinned core at this moment decides the "frame rate".
        (cpu,) = self.devices.read(GPU).cpus
        self.pins_seen.append(cpu)
        out = pathlib.Path(argv[argv.index("-output_file") + 1])
        frame_ms = 1000 / self.fps_by_cpu[cpu]
        rows = "\n".join(f"scene.exe,{frame_ms}" for _ in range(self.frames))
        out.write_text("Application,MsBetweenPresents\n" + rows + "\n", encoding="utf-8")
        return FakeProcess(2000, runs_forever=False)


class Rig:
    """An AffinityScan wired to fakes, plus everything it did."""

    def __init__(
        self, database: Database, fps_by_cpu: dict[int, float],
        initial: dict[str, AffinityPolicy] | None = None,
    ) -> None:
        self.devices = FakeDevices(initial)
        self.runner = FakeRunner(self.devices, fps_by_cpu)
        self.restarts: list[tuple[int, ...]] = []
        self.scene_pins: list[tuple[int, int]] = []
        self.progress: list = []
        self.scan = AffinityScan(
            database,
            tools=BenchTools(pathlib.Path("scene.exe"), pathlib.Path("presentmon.exe")),
            runner=self.runner,
            read=self.devices.read,
            write=self.devices.write,
            restart=lambda pnp: self.restarts.append(self.devices.read(pnp).cpus),
            set_scene_cpu=lambda pid, cpu: self.scene_pins.append((pid, cpu)),
            sleep=lambda _s: None,
        )

    def run(self, cpus=(2, 4, 6), **kwargs):
        return self.scan.run(
            ADAPTERS, list(cpus), ScanSettings(), progress=self.progress.append, **kwargs
        )


def test_every_core_is_measured_and_the_original_policy_comes_back(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700, 6: 600})

    outcome = rig.run()

    assert [r.cpu for r in outcome.results] == [2, 4, 6]
    assert selection.best(outcome.results).cpu == 4
    assert rig.runner.pins_seen == [2, 4, 6]
    assert rig.devices.policies[GPU] == AffinityPolicy()
    assert outcome.restore_error == "" and not outcome.cancelled


def test_a_policy_the_operator_chose_is_restored_not_wiped(database: Database) -> None:
    chosen = AffinityPolicy.pinned(8)
    rig = Rig(database, {2: 500, 4: 700}, {GPU: chosen})

    rig.run(cpus=(2, 4))

    assert rig.devices.policies[GPU] == chosen


def test_the_driver_is_restarted_for_each_core_and_once_more_at_the_end(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})

    rig.run(cpus=(2, 4))

    assert rig.restarts == [(2,), (4,), ()]


def test_the_scene_runs_on_the_core_being_measured(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})

    rig.run(cpus=(2, 4))

    assert [cpu for _pid, cpu in rig.scene_pins] == [2, 4]


def test_the_scene_can_be_left_to_roam(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})

    rig.scan.run(ADAPTERS, [2], ScanSettings(pin_scene=False), progress=lambda _p: None)

    assert rig.scene_pins == []


def test_every_scene_is_stopped(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})

    rig.run(cpus=(2, 4))

    assert rig.runner.scenes and all(s.stopped for s in rig.runner.scenes)


def test_stopping_keeps_the_cores_already_measured_and_restores(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700, 6: 600})

    # "Stop" is pressed once core 2 has been captured.
    outcome = rig.run(should_stop=lambda: len(rig.runner.pins_seen) >= 1)

    assert outcome.cancelled
    assert [r.cpu for r in outcome.results] == [2]
    assert rig.devices.policies[GPU] == AffinityPolicy()
    assert rig.restarts[-1] == ()


def test_stopping_mid_core_discards_it_stops_the_scene_and_restores(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})

    outcome = rig.run(should_stop=lambda: len(rig.runner.scenes) >= 1)

    assert outcome.cancelled and outcome.results == ()
    assert all(s.stopped for s in rig.runner.scenes)
    assert rig.devices.policies[GPU] == AffinityPolicy()
    assert rig.restarts[-1] == ()


def test_stopping_before_the_first_core_touches_nothing(database: Database) -> None:
    rig = Rig(database, {2: 500})

    outcome = rig.run(should_stop=lambda: True)

    assert outcome.cancelled and outcome.results == ()
    assert all(not policy.key_present for _pnp, policy in rig.devices.writes)
    assert rig.restarts == []


def test_a_scene_that_will_not_start_gives_up_after_two_cores(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700, 6: 600, 8: 600})
    rig.runner.scene_fails = True

    outcome = rig.run(cpus=(2, 4, 6, 8))

    assert [cpu for cpu, _why in outcome.failures] == [2, 4]
    assert outcome.results == ()
    assert rig.devices.policies[GPU] == AffinityPolicy()


def test_a_capture_with_too_few_frames_fails_that_core_only(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})
    rig.runner.frames = 10

    outcome = rig.run(cpus=(2, 4))

    assert outcome.results == () and len(outcome.failures) == 2


def test_the_original_policy_comes_back_even_if_the_scan_crashes(database: Database) -> None:
    rig = Rig(database, {2: 500})
    rig.runner.fps_by_cpu = {}  # KeyError inside the fake: not a PgmError

    with pytest.raises(KeyError):
        rig.run(cpus=(2,))

    assert rig.devices.policies[GPU] == AffinityPolicy()
    assert rig.restarts[-1] == ()


def test_a_failed_restore_is_reported_not_hidden(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})
    real_write = rig.devices.write
    seen = {"pinned": False}

    def write(pnp, policy):
        if policy.key_present:
            seen["pinned"] = True
        elif seen["pinned"]:
            raise AffinityError("Не удалось записать", "доступ запрещён")
        real_write(pnp, policy)

    rig.scan._write = write

    outcome = rig.run(cpus=(2, 4))

    assert "доступ запрещён" in outcome.restore_error


def test_the_scan_is_journaled_and_closed_when_it_ends(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})

    rig.run(cpus=(2, 4))

    (row,) = database.query("SELECT state, old_value FROM backups")
    assert row["state"] == "RESTORED"
    assert json.loads(row["old_value"])[GPU]["key_present"] is False


def test_a_scan_that_dies_leaves_an_interrupted_change_on_record(database: Database) -> None:
    from pudge_gaming_manager.core.optimization.recovery import find_interrupted

    scan = AffinityScan(database, tools=BenchTools(pathlib.Path("s"), pathlib.Path("p")),
                        read=lambda _p: AffinityPolicy())
    scan._open_journal({GPU: AffinityPolicy()})  # the process "dies" here

    (change,) = find_interrupted(database)
    assert change.tweak_id == GpuInterruptAffinityTweak.id


def test_progress_names_the_core_and_never_goes_backwards(database: Database) -> None:
    rig = Rig(database, {2: 500, 4: 700})

    rig.run(cpus=(2, 4))

    fractions = [p.fraction for p in rig.progress if p.fraction is not None]
    assert fractions == sorted(fractions) and fractions[-1] <= 1
    assert any("CPU 4" in p.text for p in rig.progress)


def test_the_time_estimate_grows_with_the_cores() -> None:
    settings = ScanSettings()

    assert estimate_seconds(5, settings) == 5 * estimate_seconds(1, settings)


def test_the_shipped_tools_match_their_hashes() -> None:
    from pudge_gaming_manager.core.gpu_affinity import tools

    found = tools.locate()

    assert found.scene.exists() and found.presentmon.exists()


def test_a_tampered_tool_is_refused(tmp_path: pathlib.Path) -> None:
    from pudge_gaming_manager.core.gpu_affinity import tools

    for name, _digest in (tools.SCENE, tools.PRESENTMON):
        (tmp_path / name).write_bytes(b"not the real thing")

    with pytest.raises(PgmError, match="изменён"):
        tools.locate(tmp_path)


def test_the_service_scan_tests_only_the_candidate_cores(database: Database) -> None:
    seen: dict = {}

    class RecordingScan:
        def run(self, adapters, cpus, settings, *, progress, should_stop):
            seen["cpus"] = list(cpus)
            return "outcome"

    service = _service(database, FakeDevices(), scanner=RecordingScan())

    assert service.scan(ADAPTERS, ScanSettings(), progress=lambda _p: None) == "outcome"
    assert seen["cpus"] == [2, 4, 6, 8, 10]
