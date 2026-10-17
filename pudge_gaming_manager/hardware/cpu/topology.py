"""Core topology: SMT, performance/efficiency cores, and cache domains.

``GetSystemCpuSetInformation`` returns one ``SYSTEM_CPU_SET_INFORMATION``
record per logical processor, carrying its physical core index, its
last-level-cache index and its *efficiency class*. That is enough to answer
the questions guides raise about affinity without guessing from the model
name:

* **SMT** — more logical processors than physical cores.
* **P/E cores** (Intel 12th gen and later) — more than one efficiency
  class; the highest class is the performance cores.
* **Cache domains** — more than one last-level cache; on a Ryzen 9 X3D this
  is the two CCDs, only one of which has the 3D V-Cache.

Read-only, and no affinity is ever *set* from this: the point of showing it
is that pinning a game to cores by hand is easy to get wrong on exactly
these machines.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

#: ``CpuSetInformation`` — the only record type defined.
_CPU_SET_INFORMATION = 0

#: Byte offsets inside one record (winnt.h, SYSTEM_CPU_SET_INFORMATION):
#: Size(4) Type(4) Id(4) Group(2) LogicalProcessorIndex(1) CoreIndex(1)
#: LastLevelCacheIndex(1) NumaNodeIndex(1) EfficiencyClass(1) ...
_OFF_SIZE = 0
_OFF_TYPE = 4
_OFF_GROUP = 12
_OFF_LOGICAL = 14
_OFF_CORE = 15
_OFF_LLC = 16
_OFF_EFFICIENCY = 18


@dataclass(frozen=True, slots=True)
class CpuTopology:
    logical: int = 0
    physical: int = 0
    performance_cores: int = 0
    efficiency_cores: int = 0
    cache_domains: int = 0
    error: str = ""

    @property
    def smt(self) -> bool:
        return self.physical > 0 and self.logical > self.physical

    @property
    def hybrid(self) -> bool:
        return self.efficiency_cores > 0


@dataclass(frozen=True, slots=True)
class CpuCore:
    """One physical core and the logical processors that run on it."""

    group: int
    core_index: int
    logical: tuple[int, ...]
    """Logical processor numbers within the group, ascending. With SMT the
    first is the core's primary thread, the rest are its siblings."""

    efficiency_class: int
    """Higher is faster. All equal on a CPU that is not hybrid."""

    @property
    def primary(self) -> int:
        return self.logical[0]


def _records(buffer: bytes):
    """``(group, logical, core, llc, efficiency)`` for each CPU set record."""
    offset = 0
    while offset + _OFF_EFFICIENCY < len(buffer):
        size = int.from_bytes(buffer[offset + _OFF_SIZE: offset + _OFF_SIZE + 4], "little")
        if size <= 0:
            break
        kind = int.from_bytes(buffer[offset + _OFF_TYPE: offset + _OFF_TYPE + 4], "little")
        if kind == _CPU_SET_INFORMATION:
            yield (
                int.from_bytes(buffer[offset + _OFF_GROUP: offset + _OFF_GROUP + 2], "little"),
                buffer[offset + _OFF_LOGICAL],
                buffer[offset + _OFF_CORE],
                buffer[offset + _OFF_LLC],
                buffer[offset + _OFF_EFFICIENCY],
            )
        offset += size


def parse(buffer: bytes) -> CpuTopology:
    """Decode the raw records. Pure, so it is testable without the API."""
    cores: dict[tuple[int, int], int] = {}
    caches: set[tuple[int, int]] = set()
    logical = 0
    for group, _lp, core, llc, efficiency in _records(buffer):
        logical += 1
        cores[(group, core)] = efficiency
        caches.add((group, llc))

    classes = set(cores.values())
    top = max(classes) if classes else 0
    performance = sum(1 for c in cores.values() if c == top)
    return CpuTopology(
        logical=logical,
        physical=len(cores),
        performance_cores=performance if len(classes) > 1 else len(cores),
        efficiency_cores=len(cores) - performance if len(classes) > 1 else 0,
        cache_domains=len(caches),
    )


def parse_cores(buffer: bytes) -> list[CpuCore]:
    """The physical cores, ordered by their first logical processor."""
    threads: dict[tuple[int, int], list[int]] = {}
    efficiency: dict[tuple[int, int], int] = {}
    for group, lp, core, _llc, eff in _records(buffer):
        threads.setdefault((group, core), []).append(lp)
        efficiency[(group, core)] = eff
    found = [
        CpuCore(group, core, tuple(sorted(lps)), efficiency[(group, core)])
        for (group, core), lps in threads.items()
    ]
    return sorted(found, key=lambda c: (c.group, c.primary))


def _raw() -> tuple[bytes | None, str]:
    """The CPU set buffer, or ``(None, reason)``."""
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel.GetSystemCpuSetInformation
    except (AttributeError, OSError) as exc:
        return None, str(exc)

    needed = wintypes.ULONG(0)
    query(None, 0, ctypes.byref(needed), None, 0)
    if needed.value == 0:
        return None, f"код ошибки {ctypes.get_last_error()}"
    buffer = ctypes.create_string_buffer(needed.value)
    if not query(buffer, needed, ctypes.byref(needed), None, 0):
        return None, f"код ошибки {ctypes.get_last_error()}"
    return buffer.raw[: needed.value], ""


def read() -> CpuTopology:
    """Ask Windows. Never raises."""
    raw, error = _raw()
    return CpuTopology(error=error) if raw is None else parse(raw)


def read_cores() -> list[CpuCore]:
    """The physical cores of this PC; empty when Windows does not say."""
    raw, _error = _raw()
    return [] if raw is None else parse_cores(raw)


__all__ = ["CpuCore", "CpuTopology", "parse", "parse_cores", "read", "read_cores"]
