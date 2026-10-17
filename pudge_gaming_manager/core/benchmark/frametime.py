"""Frame-rate statistics from a PresentMon capture.

PresentMon writes one CSV row per presented frame, with the time since the
previous one in milliseconds. From those times come the numbers a frame-time
comparison is read by:

* **average** — frames over total time (the harmonic mean of the instant
  rates, not their arithmetic mean: a slow frame lasts longer, so it counts
  for more).
* **1 % / 0.1 % low** — the rate of the frame at which the slowest frames,
  taken together, have used up 1 % (0.1 %) of the whole capture's time. A
  stutter shows here long before it moves the average.
* **stdev** — spread of the instant rates; steadier is smaller.

Pure functions over numbers, so the maths is tested without a GPU.
"""

from __future__ import annotations

import csv
import math
import pathlib
from dataclasses import dataclass
from typing import Sequence

#: Fewer frames than this say nothing about 0.1 % lows.
MIN_FRAMES = 100


@dataclass(frozen=True, slots=True)
class FrameStats:
    frames: int
    average: float
    minimum: float
    maximum: float
    stdev: float
    low_1: float
    low_01: float


def _lows(slowest_first: Sequence[float], total_ms: float, percent: float) -> float:
    budget = percent / 100 * total_ms
    used = 0.0
    for frame_ms in slowest_first:
        used += frame_ms
        if used >= budget:
            return 1000 / frame_ms
    return 1000 / slowest_first[-1]


def stats(frametimes_ms: Sequence[float]) -> FrameStats | None:
    """Statistics for a capture, or ``None`` when it has too few frames."""
    frames = [f for f in frametimes_ms if f > 0]
    if len(frames) < MIN_FRAMES:
        return None

    slowest_first = sorted(frames, reverse=True)
    total = sum(frames)
    average = 1000 * len(frames) / total
    spread = math.sqrt(
        sum((1000 / f - average) ** 2 for f in frames) / (len(frames) - 1)
    )
    return FrameStats(
        frames=len(frames),
        average=average,
        minimum=1000 / slowest_first[0],
        maximum=1000 / slowest_first[-1],
        stdev=spread,
        low_1=_lows(slowest_first, total, 1),
        low_01=_lows(slowest_first, total, 0.1),
    )


def read_presentmon_csv(path: pathlib.Path | str) -> list[float]:
    """The frame times (ms) in a PresentMon CSV; empty if it has none.

    PresentMon renamed the column between versions (``msBetweenPresents``,
    then ``MsBetweenPresents``), so the header is matched without case.
    """
    times: list[float] = []
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            lowered = {(k or "").lower(): v for k, v in row.items()}
            try:
                times.append(float(lowered["msbetweenpresents"]))
            except (KeyError, TypeError, ValueError):
                continue
    return times


__all__ = ["MIN_FRAMES", "FrameStats", "read_presentmon_csv", "stats"]
