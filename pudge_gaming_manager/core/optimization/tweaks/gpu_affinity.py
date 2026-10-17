"""Pin the graphics adapter's interrupts to one processor core.

The change AutoGpuAffinity's benchmark exists to choose and GoInterruptPolicy
makes: the adapter's ``Affinity Policy`` set to "these processors only", the
core named, and the interrupt priority High. The «Ядра GPU» tab picks the
core by measurement; this tweak only writes it, so the write goes through the
same backup, verification and history as every other change.

It takes effect when the adapter next starts — after a driver restart or a
reboot — which is why the result says so and why a restart is a separate,
confirmed step rather than part of the write.

The backup is JSON in ``old_value``: for each adapter, the policy as it was,
including which of its three values did not exist. Rollback writes exactly
that back, so an adapter that had no policy ends with no policy.
"""

from __future__ import annotations

import json
from typing import Callable, Sequence

from ....utilities.exceptions import PgmError
from ....windows.devices import affinity
from ....windows.devices.affinity import AffinityPolicy
from ..tweak import (
    ApplyResult,
    BackupRecord,
    BackupScope,
    Outcome,
    RiskLevel,
    Tweak,
    TweakContext,
    TweakState,
    Validation,
    Verification,
)


def encode_policies(policies: dict[str, AffinityPolicy]) -> str:
    return json.dumps({k: v.to_dict() for k, v in policies.items()}, sort_keys=True)


def decode_policies(text: str) -> dict[str, AffinityPolicy]:
    return {k: AffinityPolicy.from_dict(v) for k, v in json.loads(text or "{}").items()}


class GpuInterruptAffinityTweak(Tweak):
    id = "devices.gpu.interrupt_affinity"
    version = 1
    name = "Прерывания видеокарты: закрепить за ядром"
    description = (
        "Задаёт видеокарте политику «обрабатывать прерывания только на этом "
        "ядре процессора» с высоким приоритетом — как в GoInterruptPolicy. "
        "Ядро выбирается на вкладке «Ядра GPU» по замеру."
    )
    rationale = (
        "По умолчанию Windows сама решает, на каком ядре обработать прерывание "
        "видеокарты, и часто это ядро 0, на котором уже работает почти вся "
        "система. Отдельное тихое ядро может выровнять время кадра и поднять "
        "1 % low, но выигрыш зависит от процессора и драйвера — поэтому ядро "
        "выбирается замером, а не по умолчанию. Значение вступает в силу после "
        "перезапуска видеодрайвера или перезагрузки; откат возвращает прежнюю "
        "политику, включая её отсутствие."
    )
    risk = RiskLevel.MEDIUM
    subsystem = "gpu"
    scope = BackupScope.DEVICE
    requires_admin = True

    def __init__(
        self,
        adapters: Sequence[tuple[str, str]] = (),
        cpu: int | None = None,
        reader: Callable[[str], AffinityPolicy] = affinity.read,
        writer: Callable[[str, AffinityPolicy], None] = affinity.write,
    ) -> None:
        """
        Args:
            adapters: ``(name, PnP instance id)`` of each adapter to pin.
            cpu: Logical processor to pin to. Unneeded to roll a change back.
        """
        self._adapters = list(adapters)
        self._cpu = cpu
        self._read = reader
        self._write = writer

    def _desired(self) -> AffinityPolicy:
        if self._cpu is None:
            raise affinity.AffinityError("Не выбрано ядро процессора")
        return AffinityPolicy.pinned(self._cpu)

    def _current(self) -> dict[str, AffinityPolicy]:
        return {pnp: self._read(pnp) for _name, pnp in self._adapters}

    def scan(self, ctx: TweakContext) -> TweakState:
        try:
            desired = self._desired()
            current = self._current()
        except PgmError as exc:
            return TweakState(
                needs_change=False, current_absent=True,
                summary=f"{self.name}: {exc.reason or exc.what}",
            )
        wanted = {pnp: desired for pnp in current}
        changing = [
            f"{name}: {current[pnp].label} → {desired.label}"
            for name, pnp in self._adapters
            if current[pnp] != desired
        ]
        return TweakState(
            current_value=encode_policies(current),
            desired_value=encode_policies(wanted),
            needs_change=bool(changing),
            summary=(
                f"{self.name} — " + "; ".join(changing)
                if changing
                else f"{self.name}: уже закреплено за CPU {self._cpu}"
            ),
        )

    def validate(self, ctx: TweakContext, state: TweakState) -> Validation:
        if state.current_absent:
            return Validation.refuse("Не удалось прочитать политику прерываний видеокарты.")
        if not self._adapters:
            return Validation.refuse("Не выбрана ни одна видеокарта.")
        return Validation.allow(requires_admin=True)

    def backup(self, ctx: TweakContext, state: TweakState) -> BackupRecord:
        return BackupRecord(
            backup_id=BackupRecord.new_id(),
            tweak_id=self.id,
            tweak_version=self.version,
            scope=BackupScope.DEVICE,
            target=self.id,
            old_value=state.current_value,
            old_value_kind="JSON_AFFINITY_POLICY",
            new_value=state.desired_value,
        )

    def apply(self, ctx: TweakContext, state: TweakState) -> ApplyResult:
        desired = self._desired()
        try:
            for _name, pnp in self._adapters:
                self._write(pnp, desired)
        except PgmError as exc:
            return ApplyResult(Outcome.FAILED, exc.reason or exc.what)
        return ApplyResult(
            Outcome.SUCCESS,
            f"Прерывания видеокарты закреплены за CPU {self._cpu}; вступит в "
            "силу после перезапуска видеодрайвера или перезагрузки.",
            needs_restart=True,
        )

    def verify(self, ctx: TweakContext, state: TweakState) -> Verification:
        wanted = decode_policies(str(state.desired_value))
        try:
            now = self._current()
        except PgmError as exc:
            return Verification.deny(None, exc.reason or exc.what)
        wrong = [pnp for pnp, policy in wanted.items() if now.get(pnp) != policy]
        if wrong:
            return Verification.deny(encode_policies(now), "Реестр не сохранил политику.")
        return Verification.confirm(
            encode_policies(now), "Политика прочитана из реестра и совпадает с заданной."
        )

    def rollback(self, ctx: TweakContext, backup: BackupRecord) -> ApplyResult:
        previous = decode_policies(str(backup.old_value or "{}"))
        failed: list[str] = []
        for pnp, policy in previous.items():
            try:
                self._write(pnp, policy)
            except PgmError as exc:
                failed.append(exc.reason or exc.what)
        if failed:
            return ApplyResult(Outcome.FAILED, "; ".join(failed))
        return ApplyResult(
            Outcome.SUCCESS,
            "Прежняя политика прерываний возвращена; вступит в силу после "
            "перезапуска видеодрайвера или перезагрузки.",
            needs_restart=True,
        )


__all__ = ["GpuInterruptAffinityTweak", "decode_policies", "encode_policies"]
