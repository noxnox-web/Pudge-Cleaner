"""What stops the window from closing, or an update from replacing it."""

from __future__ import annotations

from PySide6.QtWidgets import QMainWindow


def busy_reason(window: "QMainWindow") -> str | None:
    """What is changing the system right now, or ``None``.

    A write in progress must not be interrupted -- not by closing the window
    and not by an update replacing the program: applying a tweak or removing
    games between the change and its verification could strand the system
    half-done. Scans and profile reads only read, so closing waits for them
    (shutdown joins the worker). Evaluated in this order and stopping at the
    first hit, so a caller may hold only the attributes up to that one.
    """
    return (
        "Применяются изменения."
        if window._optimizer.applying
        else "Идёт очистка Стима."
        if window._steam.wiping
        else "Идёт очистка диска."
        if window._cleanup.cleaning
        else "Идёт удаление или перенос папки."
        if window._tools.modifying
        else "Применяются или откатываются настройки."
        if window._system.applying
        else window._affinity_tab.blocks_close()
    )


def close_blocker(window: "QMainWindow") -> str | None:
    return busy_reason(window) or (
        "Скачивается и ставится обновление." if window._updates.installing else None
    )


__all__ = ["busy_reason", "close_blocker"]
