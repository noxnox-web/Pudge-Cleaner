"""The two programs the benchmark runs, shipped with Pudge Cleaner.

* ``lava-triangle.exe`` — a Vulkan scene (liblava, MIT) that draws as fast as
  the GPU allows, so frame time is dominated by how well the driver and its
  interrupts are served.
* ``PresentMon`` (Intel, MIT) — records when each frame was presented.

They are the same two programs AutoGpuAffinity uses. They run elevated, so a
file is checked against the hash it was shipped with before every run: a
swapped executable in a writable install folder would otherwise run as
Administrator.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass

from ...utilities.exceptions import PgmError

_DIR = pathlib.Path(__file__).resolve().parents[2] / "vendor" / "gpu_affinity"

SCENE = ("lava-triangle.exe",
         "f3647ec482df5817f0cfc051246192b70d4008dcdb0ca6fb923b294a85b7e10e")
PRESENTMON = ("PresentMon-1.10.0-x64.exe",
              "e57a2f8ee1de1ef1a5516d875f1b115e881943cd729fe9c5a2f88b1dc79a8a3b")


@dataclass(frozen=True, slots=True)
class BenchTools:
    scene: pathlib.Path
    presentmon: pathlib.Path

    @property
    def scene_process(self) -> str:
        """The scene's process name, which PresentMon is told to watch."""
        return self.scene.name


def _verified(directory: pathlib.Path, name: str, digest: str) -> pathlib.Path:
    path = directory / name
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PgmError(
            f"Не найден тестовый компонент «{name}»",
            str(exc.strerror or exc),
            remedy="Переустановите Pudge Cleaner: файл входит в его комплект.",
        ) from exc
    if actual != digest:
        raise PgmError(
            f"Тестовый компонент «{name}» изменён",
            "Его контрольная сумма не совпадает с той, с которой он поставлялся.",
            remedy="Переустановите Pudge Cleaner.",
        )
    return path


def locate(directory: pathlib.Path | None = None) -> BenchTools:
    """The verified tools. Raises :class:`PgmError` if one is missing or altered."""
    where = directory or _DIR
    return BenchTools(
        scene=_verified(where, *SCENE),
        presentmon=_verified(where, *PRESENTMON),
    )


__all__ = ["BenchTools", "PRESENTMON", "SCENE", "locate"]
