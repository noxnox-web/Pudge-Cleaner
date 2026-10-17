"""Application services: how the layers above ``core`` obtain working objects.

The GUI has no business opening a database or deciding where it lives. It
asks for a pipeline and gets one, fully wired. This keeps the storage
location, migration timing and connection lifetime inside ``core`` where
they belong — and keeps the ``app`` layer free of any ``database`` import,
which the architecture test enforces.
"""

from __future__ import annotations

import pathlib
import threading

from ..database.connection import Database
from ..hardware.models import HardwareSnapshot
from ..utilities.logging_setup import get_logger
from ..windows.cleanup.rules import CleanupCategory
from .audit.report import SystemReport, build_report
from .gpu_affinity.service import GpuAffinityService
from .optimization import history, restore_point
from .optimization.pipeline import OptimizationPipeline
from .optimization.recovery import InterruptedChange, acknowledge, find_interrupted
from .settings.service import SettingsService

_log = get_logger(__name__)

_database: Database | None = None
_lock = threading.Lock()


def default_database(path: pathlib.Path | str | None = None) -> Database:
    """Return the process-wide database, opening it on first use.

    One instance per process: ``Database`` already keeps a connection per
    thread, and opening a second instance against the same file would mean
    two migration runs racing each other at startup.
    """
    global _database
    with _lock:
        if _database is None or path is not None:
            _database = Database(path)
            _log.info("database ready at %s", _database.path)
        return _database


def create_optimization_pipeline(
    *,
    allow_risk_above_low: bool = False,
    cleanup_categories: tuple[CleanupCategory, ...] | None = None,
    database: Database | None = None,
) -> OptimizationPipeline:
    """Build a pipeline backed by the local database.

    Args:
        allow_risk_above_low: Permit MEDIUM and higher tweaks. Off by
            default so a caller must opt in deliberately (rule #38).
        cleanup_categories: Override which cleanup categories participate.
        database: Injected in tests; production uses the shared instance.
    """
    return OptimizationPipeline(
        database or default_database(),
        allow_risk_above_low=allow_risk_above_low,
        cleanup_categories=cleanup_categories,
        restore_point=restore_point.create,
    )


def create_settings_service(database: Database | None = None) -> SettingsService:
    """The «Настройки Windows» service, backed by the local database."""
    return SettingsService(
        database or default_database(), restore_point=restore_point.create
    )


def create_gpu_affinity_service(database: Database | None = None) -> GpuAffinityService:
    """The «Ядра GPU» service, backed by the local database."""
    return GpuAffinityService(
        database or default_database(), restore_point=restore_point.create
    )


def change_history() -> list[history.ChangeRecord]:
    """Every change PGM recorded on this PC, newest first."""
    return history.list_changes(default_database())


def restore_change(backup_id: str) -> history.RestoreResult:
    """Undo one recorded change."""
    return history.restore(default_database(), backup_id)


def restore_all_changes() -> list[history.RestoreResult]:
    """Undo every change still in force, newest first."""
    return history.restore_all(default_database())


def system_audit(snapshot: HardwareSnapshot | None = None) -> SystemReport:
    """The read-only system audit."""
    return build_report(snapshot)


def interrupted_changes() -> list[InterruptedChange]:
    """Changes a previous run started but never verified or undone."""
    return find_interrupted(default_database())


def acknowledge_interrupted(changes: list[InterruptedChange]) -> None:
    """Record that the operator has been shown these interrupted changes."""
    acknowledge(default_database(), changes)


def reset_for_tests() -> None:
    """Drop the cached database so a test can point at a temporary file."""
    global _database
    with _lock:
        if _database is not None:
            _database.close()
        _database = None


__all__ = [
    "acknowledge_interrupted",
    "change_history",
    "create_gpu_affinity_service",
    "create_optimization_pipeline",
    "create_settings_service",
    "default_database",
    "interrupted_changes",
    "reset_for_tests",
    "restore_all_changes",
    "restore_change",
    "system_audit",
]
