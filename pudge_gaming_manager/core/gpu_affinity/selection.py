"""Which cores are worth testing, and which result is the best.

The rules come from how the video guide and AutoGpuAffinity's own README use
the tool, made explicit:

* Only a physical core's *first* thread is tested. Its SMT sibling shares the
  same execution units, so testing both measures the same core twice.
* The core that holds logical processor 0 is left out. Windows runs most of
  the system's own work there, which is exactly what moving the interrupts
  away from is meant to avoid — even when it posts the best number.
* On a hybrid CPU only the fastest class is tried. A graphics interrupt on
  an efficiency core is a slower interrupt.

Ranking is by three measures together — average, 1 % low and 0.1 % low —
because a core that wins the average but stutters is not a better core.
Pure functions, tested without a GPU.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ...hardware.cpu.topology import CpuCore
from ...windows.devices.affinity import MAX_CPU
from ..benchmark.frametime import FrameStats

_MEASURES = ("average", "low_1", "low_01")


def candidate_cpus(cores: Sequence[CpuCore]) -> list[int]:
    """Logical processors to benchmark, ascending."""
    group0 = [c for c in cores if c.group == 0 and c.primary <= MAX_CPU]
    if not group0:
        return []
    fastest = max(c.efficiency_class for c in group0)
    return [
        c.primary
        for c in group0
        if c.efficiency_class == fastest and 0 not in c.logical
    ]


def main_core(cores: Sequence[CpuCore]) -> CpuCore | None:
    """The physical core that runs logical processor 0."""
    return next((c for c in cores if c.group == 0 and 0 in c.logical), None)


@dataclass(frozen=True, slots=True)
class CoreResult:
    cpu: int
    stats: FrameStats


def ranked(results: Sequence[CoreResult]) -> list[CoreResult]:
    """Best first: lowest total rank over the three measures, then average."""
    totals = {r.cpu: 0 for r in results}
    for measure in _MEASURES:
        values = [getattr(r.stats, measure) for r in results]
        for r in results:
            mine = getattr(r.stats, measure)
            totals[r.cpu] += sum(1 for v in values if v > mine)
    return sorted(results, key=lambda r: (totals[r.cpu], -r.stats.average))


def best(results: Sequence[CoreResult]) -> CoreResult | None:
    order = ranked(results)
    return order[0] if order else None


def spread_percent(results: Sequence[CoreResult]) -> float:
    """How far the best core's average is above the worst one's, in percent.

    Small spreads are measurement noise, and the tab says so: a 1 % lead
    between two cores is not a reason to move anything.
    """
    averages = [r.stats.average for r in results]
    if len(averages) < 2 or min(averages) <= 0:
        return 0.0
    return (max(averages) / min(averages) - 1) * 100


__all__ = ["CoreResult", "best", "candidate_cpus", "main_core", "ranked", "spread_percent"]
