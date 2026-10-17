"""Change history, and undoing a change after the run that made it.

The engine already rolls a change back when its verification fails, inside
the same run. This module is for the other case: the change verified, the
operator lived with it, and now wants it gone — an hour later, or after a
restart, or from the history dialog on a PC somebody else optimised.

Everything needed is already on disk. Every reversible change wrote a
``backups`` row *before* it mutated anything, holding the old value, its
type, whether it existed at all, and the tweak that made it. Undoing is
therefore the tweak's own ``rollback`` fed that row — the same code path the
engine uses, with the same read-back verification — not a second, generic
restorer that could disagree with it.

Order matters
-------------
Two changes to the same setting stack: A→B, then B→C. Undoing only the
older one would write A over C and leave the history claiming C's change is
still in force. So a single undo is refused while a newer change to the same
target is still applied, and "undo everything" goes newest first, which
unwinds any stack correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ...database.connection import Database
from ...utilities.logging_setup import get_logger
from ..settings.catalog import SETTINGS_BY_ID, SETTINGS_BY_TWEAK_ID
from .store import RunStore
from .tweak import BackupRecord, BackupScope, Outcome, Tweak, TweakContext
from .tweaks.choice import tweak_for
from .tweaks.devices import (
    DisableEnergyEfficientEthernetTweak,
    DisableNicPowerSavingTweak,
    DisableUsbPowerSavingTweak,
)
from .tweaks.display import SetRefreshRateTweak
from .tweaks.dota_hosts import BlockDotaWebTweak
from .tweaks.game_dvr import DisableGameDvrPolicyTweak, DisableGameDvrTweak
from .tweaks.gpu_affinity import GpuInterruptAffinityTweak
from .tweaks.mouse import DisableMouseAccelerationTweak
from .tweaks.power import SetPowerPlanTweak
from .tweaks.power_settings import (
    DisablePcieAspmTweak,
    DisableUsbSelectiveSuspendTweak,
)

_log = get_logger(__name__)

#: Registry kinds whose backup text is a number.
_INTEGER_KINDS = frozenset({"REG_DWORD", "REG_QWORD"})


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """One change PGM made, as recorded before it was made."""

    backup_id: str
    tweak_id: str
    name: str
    created_at: str
    scope: str
    target: str
    old_value: str | None
    old_value_absent: bool
    old_value_kind: str
    new_value: str | None
    state: str
    restored_at: str | None

    @property
    def is_applied(self) -> bool:
        return self.state == "APPLIED"

    @property
    def state_label(self) -> str:
        return {
            "APPLIED": "действует",
            "RESTORED": "откачено",
            "FAILED": "не подтвердилось",
            "APPLYING": "прервано",
        }.get(self.state, self.state)

    def line(self) -> str:
        before = "не задано" if self.old_value_absent else (self.old_value or "—")
        return (
            f"{self.created_at[:19].replace('T', ' ')}  [{self.state_label}]  "
            f"{self.name}: {before} → {self.new_value or '—'}"
        )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    backup_id: str
    name: str
    ok: bool
    message: str


def _registry_value_tweak(spec_id: str) -> Callable[[ChangeRecord], Tweak]:
    spec = SETTINGS_BY_ID[spec_id]
    return lambda _r: tweak_for(spec, spec.options[0].key)


#: Tweaks whose rollback needs nothing but the backup row.
_FACTORIES: dict[str, Callable[[ChangeRecord], Tweak]] = {
    DisableGameDvrTweak.id: lambda _r: DisableGameDvrTweak(),
    DisableGameDvrPolicyTweak.id: lambda _r: DisableGameDvrPolicyTweak(),
    DisableMouseAccelerationTweak.id: lambda _r: DisableMouseAccelerationTweak(),
    SetPowerPlanTweak.id: lambda _r: SetPowerPlanTweak(),
    DisableUsbSelectiveSuspendTweak.id: lambda _r: DisableUsbSelectiveSuspendTweak(),
    DisablePcieAspmTweak.id: lambda _r: DisablePcieAspmTweak(),
    SetRefreshRateTweak.id: lambda r: SetRefreshRateTweak(r.target, r.target),
    BlockDotaWebTweak.id: lambda _r: BlockDotaWebTweak(),
    DisableUsbPowerSavingTweak.id: lambda _r: DisableUsbPowerSavingTweak(),
    DisableNicPowerSavingTweak.id: lambda _r: DisableNicPowerSavingTweak(),
    DisableEnergyEfficientEthernetTweak.id: lambda _r: DisableEnergyEfficientEthernetTweak(),
    GpuInterruptAffinityTweak.id: lambda _r: GpuInterruptAffinityTweak(),
    # Ids the same registry values had before they moved into the settings
    # catalogue. Old history rows must stay undoable.
    "graphics.gpu_scheduling.enable": _registry_value_tweak("hags"),
    "network.throttling.disable": _registry_value_tweak("network_throttling"),
}

def _tweak_for(record: ChangeRecord) -> Tweak | None:
    spec = SETTINGS_BY_TWEAK_ID.get(record.tweak_id)
    if spec is not None:
        return tweak_for(spec, spec.options[0].key)
    factory = _FACTORIES.get(record.tweak_id)
    return factory(record) if factory else None


def decode(text: str | None, kind: str) -> Any:
    """Turn a backup's stored text back into the value rollback expects.

    Backups store text. For registry numbers that text must become an int
    again, or rollback would write the string ``"10"`` where a DWORD was.
    Every other kind is handed over as text, which is what those tweaks'
    own rollbacks parse.
    """
    if text is None:
        return None
    if kind in _INTEGER_KINDS:
        try:
            return int(text)
        except ValueError:
            return text
    return text


def _scope(text: str) -> BackupScope:
    try:
        return BackupScope(text)
    except ValueError:
        return BackupScope.REGISTRY


def _record(row: Any) -> ChangeRecord:
    return ChangeRecord(
        backup_id=row["backup_id"],
        tweak_id=row["tweak_id"],
        name=row["name"] or row["tweak_id"],
        created_at=row["created_at"] or "",
        scope=row["scope"],
        target=row["target"],
        old_value=row["old_value"],
        old_value_absent=bool(row["old_value_absent"]),
        old_value_kind=row["old_value_kind"] or "",
        new_value=row["new_value"],
        state=row["state"],
        restored_at=row["restored_at"],
    )


_SELECT = (
    "SELECT b.backup_id, b.tweak_id, b.created_at, b.scope, b.target, "
    "b.old_value, b.old_value_absent, b.old_value_kind, b.new_value, b.state, "
    "b.restored_at, (SELECT t.name FROM tweaks t WHERE t.tweak_id = b.tweak_id "
    "ORDER BY t.version DESC LIMIT 1) AS name FROM backups b"
)


def list_changes(database: Database, *, limit: int = 500) -> list[ChangeRecord]:
    """Every recorded change, newest first."""
    rows = database.query(
        _SELECT + " ORDER BY b.created_at DESC, b.rowid DESC LIMIT ?", (limit,)
    )
    return [_record(row) for row in rows]


def _newer_applied(database: Database, record: ChangeRecord) -> bool:
    row = database.query_one(
        # By target, not tweak id: a registry value set under an old id and
        # again under the catalogue's is still one stack.
        "SELECT 1 FROM backups WHERE state='APPLIED' AND "
        "target=? AND backup_id<>? AND (created_at > ? OR (created_at = ? "
        "AND rowid > (SELECT rowid FROM backups WHERE backup_id=?))) LIMIT 1",
        (
            record.target, record.backup_id,
            record.created_at, record.created_at, record.backup_id,
        ),
    )
    return row is not None


def restore(
    database: Database,
    backup_id: str,
    *,
    context: TweakContext | None = None,
    check_newer: bool = True,
    resolve: Callable[[ChangeRecord], Tweak | None] | None = None,
) -> RestoreResult:
    """Undo one applied change, verified like any rollback.

    Args:
        resolve: Finds the tweak whose rollback undoes a record. Injected in
            tests so they never touch the real registry (rule #48).
    """
    row = database.query_one(_SELECT + " WHERE b.backup_id=?", (backup_id,))
    if row is None:
        return RestoreResult(backup_id, "?", False, "Такой записи нет.")
    record = _record(row)

    if not record.is_applied:
        return RestoreResult(
            backup_id, record.name, False,
            f"Изменение уже в состоянии «{record.state_label}» — откатывать нечего.",
        )
    if record.scope == BackupScope.NONE.value:
        return RestoreResult(backup_id, record.name, False, "Это изменение необратимо.")
    if check_newer and _newer_applied(database, record):
        return RestoreResult(
            backup_id, record.name, False,
            "Эту настройку потом меняли ещё раз. Сначала откатите более "
            "позднее изменение — иначе вернётся не то значение.",
        )

    tweak = (resolve or _tweak_for)(record)
    if tweak is None:
        return RestoreResult(
            backup_id, record.name, False,
            "Программа не знает, как откатить это изменение (твик удалён или "
            "переименован). Старое значение: "
            f"{'не задано' if record.old_value_absent else record.old_value}.",
        )

    backup = BackupRecord(
        backup_id=record.backup_id,
        tweak_id=record.tweak_id,
        tweak_version=0,
        scope=_scope(record.scope),
        target=record.target,
        old_value=decode(record.old_value, record.old_value_kind),
        old_value_absent=record.old_value_absent,
        old_value_kind=record.old_value_kind,
        new_value=record.new_value,
    )
    store = RunStore(database)
    try:
        result = tweak.rollback(context or TweakContext(database=database), backup)
    except Exception as exc:  # noqa: BLE001 - reported, never raised into the UI
        _log.exception("restore of %s failed", backup_id)
        result = None
        message = str(exc)
    else:
        message = result.detail

    ok = result is not None and result.outcome is Outcome.SUCCESS
    if ok:
        store.mark_backup(backup_id, "RESTORED", finished=True)
    store.audit(
        "history", "restore", target=record.target,
        old_state=record.new_value,
        new_state=None if record.old_value_absent else record.old_value,
        result="SUCCESS" if ok else "FAILED",
        error=None if ok else message,
    )
    return RestoreResult(backup_id, record.name, ok, message)


def restore_all(
    database: Database,
    *,
    context: TweakContext | None = None,
    resolve: Callable[[ChangeRecord], Tweak | None] | None = None,
) -> list[RestoreResult]:
    """Undo every applied change, newest first."""
    results = []
    for record in list_changes(database, limit=100_000):
        if record.is_applied and record.scope != BackupScope.NONE.value:
            # Newest first already unwinds stacks, so the per-item guard
            # would only refuse what this loop is about to handle in order.
            results.append(
                restore(
                    database, record.backup_id, context=context,
                    check_newer=False, resolve=resolve,
                )
            )
    return results


__all__ = [
    "ChangeRecord",
    "RestoreResult",
    "decode",
    "list_changes",
    "restore",
    "restore_all",
]
