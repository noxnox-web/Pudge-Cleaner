"""The Tweak contract: the unit of system change.

Every modification PGM makes to a PC implements this interface. The engine —
not the tweak — enforces the ordering and the safety invariants, so a new
tweak cannot forget to take a backup or skip verification.

A tweak that cannot describe, undo and verify its change does not belong in
this system (rule #64).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


#: The one drift result the engine treats as "nothing to do" rather than
#: "something moved, stand down". It is a sentinel, not a message: the
#: engine compares against this object, so the wording below can be
#: translated or reworded without changing which outcome is recorded.
#: Deciding NOT_NEEDED vs SKIPPED by matching display text is precisely the
#: bug this replaces.
ALREADY_DESIRED = "уже в нужном состоянии"


class RiskLevel(str, Enum):
    """How much damage a change could do if it goes wrong (rule #38)."""

    SAFE = "SAFE"
    """No functional effect beyond the intended one. e.g. deleting temp files."""

    LOW = "LOW"
    """Reversible, well-understood, affects only performance behaviour."""

    MEDIUM = "MEDIUM"
    """Changes a user-visible system setting. Requires admin and opt-in."""

    HIGH = "HIGH"
    """Could disrupt a running session or a dependent application."""

    CRITICAL = "CRITICAL"
    """Could leave the machine unusable. Never auto-applied under any policy."""

    @property
    def auto_applicable(self) -> bool:
        """SAFE and LOW may run without explicit per-tweak consent."""
        return self in (RiskLevel.SAFE, RiskLevel.LOW)

    @property
    def rank(self) -> int:
        return _RISK_ORDER[self]


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.SAFE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class BackupScope(str, Enum):
    REGISTRY = "REGISTRY"
    SERVICE = "SERVICE"
    POWER = "POWER"
    DISPLAY = "DISPLAY"
    FILE = "FILE"
    DEVICE = "DEVICE"
    """A device's power-management flag (``MSPower_DeviceEnable``)."""

    NETWORK = "NETWORK"
    """A network adapter's advanced property."""

    NONE = "NONE"
    """The change is inherently irreversible *and* loses nothing, e.g.
    deleting a temporary file that the system regenerates."""


class Phase(str, Enum):
    SCAN = "SCAN"
    VALIDATE = "VALIDATE"
    BACKUP = "BACKUP"
    APPLY = "APPLY"
    VERIFY = "VERIFY"
    ROLLBACK = "ROLLBACK"


class Outcome(str, Enum):
    SUCCESS = "SUCCESS"
    """The configuration changed and re-reading confirmed it.

    This says nothing about frame rate (rule #65).
    """

    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ROLLED_BACK = "ROLLED_BACK"
    NOT_NEEDED = "NOT_NEEDED"
    """Already in the desired state. Not a failure, and not a change."""


@dataclass(frozen=True, slots=True)
class TweakState:
    """What :meth:`Tweak.scan` observed."""

    current_value: Any = None
    desired_value: Any = None
    needs_change: bool = False
    current_absent: bool = False
    """The setting does not exist at all, as distinct from existing empty.

    Carried all the way into the backup record so rollback can restore
    absence rather than writing an empty value (ARCHITECTURE §5).
    """

    summary: str = ""
    """One line for the dry-run preview, e.g. 'Power plan: Balanced -> High performance'."""


@dataclass(frozen=True, slots=True)
class Validation:
    """Whether the change can be attempted on this machine."""

    ok: bool
    reason: str = ""
    requires_admin: bool = False

    @classmethod
    def allow(cls, *, requires_admin: bool = False) -> "Validation":
        return cls(ok=True, requires_admin=requires_admin)

    @classmethod
    def refuse(cls, reason: str) -> "Validation":
        return cls(ok=False, reason=reason)


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """Prior state, durable before any mutation."""

    backup_id: str
    tweak_id: str
    tweak_version: int
    scope: BackupScope
    target: str
    old_value: Any = None
    old_value_absent: bool = False
    old_value_kind: str = ""
    new_value: Any = None

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class ApplyResult:
    outcome: Outcome
    detail: str = ""
    needs_restart: bool = False


@dataclass(frozen=True, slots=True)
class Verification:
    """Did re-reading the system confirm the change?"""

    confirmed: bool
    observed_value: Any = None
    detail: str = ""

    @classmethod
    def confirm(cls, observed: Any = None, detail: str = "") -> "Verification":
        return cls(confirmed=True, observed_value=observed, detail=detail)

    @classmethod
    def deny(cls, observed: Any = None, detail: str = "") -> "Verification":
        return cls(confirmed=False, observed_value=observed, detail=detail)


@dataclass(slots=True)
class TweakContext:
    """Everything a tweak needs from its environment.

    Injected rather than imported so tests can supply fakes and never touch
    the real machine (rule #48).
    """

    runner: Any = None
    powershell: Any = None
    database: Any = None
    dry_run: bool = False
    run_id: str = ""
    extras: dict[str, Any] = field(default_factory=dict)
    progress: Callable[[float | None, str], None] | None = None
    """Set by the engine while a change is being applied; see ``report_progress``."""

    def report_progress(self, fraction: float | None, detail: str = "") -> None:
        """Tell the operator how far a long ``apply`` has got.

        ``fraction`` is 0..1 of this change, or ``None`` when it cannot tell.
        A no-op outside a run, so a tweak may call it unconditionally.
        """
        if self.progress is not None:
            self.progress(fraction, detail)


class Tweak(ABC):
    """One reversible, verifiable system change.

    Subclasses implement the observation and mutation; :class:`TweakEngine`
    owns the ordering, the backup guarantee and the rollback-on-failure rule.
    """

    id: str = ""
    version: int = 1
    name: str = ""
    description: str = ""
    rationale: str = ""
    """Why this change helps. Required — rule #64 forbids unexplained tweaks."""

    risk: RiskLevel = RiskLevel.MEDIUM
    subsystem: str = ""
    scope: BackupScope = BackupScope.NONE
    requires_admin: bool = False

    #: Lifecycle methods a concrete tweak must implement.
    _LIFECYCLE = ("scan", "backup", "apply", "verify", "rollback")

    abstract_base: bool = False
    """Set on a class that implements the lifecycle but is not itself a
    usable tweak. Never inherited as True: the check reads it from the
    class's own ``__dict__``, so a concrete subclass of an abstract base
    must still declare its id, name and rationale."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Catch an unusable tweak at import time, not when an operator runs it
        # against a club PC.
        #
        # ``__abstractmethods__`` is populated by ABCMeta *after* this hook
        # runs, so concreteness is determined by checking the lifecycle
        # methods directly: an inherited abstract method still carries
        # ``__isabstractmethod__``, an override does not.
        is_concrete = not any(
            getattr(getattr(cls, name, None), "__isabstractmethod__", False)
            for name in Tweak._LIFECYCLE
        )
        if not is_concrete:
            return

        # A shared base can implement the whole lifecycle and still not be a
        # tweak: RegistryValueTweak writes "one registry value", and which
        # value is the subclass's business. Such a base says so explicitly,
        # which keeps the rule intact — nothing is exempt by accident, only
        # by declaring itself unusable on its own.
        if cls.__dict__.get("abstract_base", False):
            return

        missing = [
            attribute
            for attribute in ("id", "name", "rationale")
            if not getattr(cls, attribute, "")
        ]
        if missing:
            raise TypeError(
                f"{cls.__name__} must define {', '.join(missing)}: every "
                "tweak has to explain itself (rule #64)."
            )

    @property
    def versioned_id(self) -> str:
        return f"{self.id}@{self.version}"

    @abstractmethod
    def scan(self, ctx: TweakContext) -> TweakState:
        """Observe current state. Must not mutate anything."""

    def validate(self, ctx: TweakContext, state: TweakState) -> Validation:
        """Decide whether applying is safe and possible on this machine."""
        return Validation.allow(requires_admin=self.requires_admin)

    def drift_since_plan(self, ctx: TweakContext, planned: TweakState) -> str | None:
        """Why the system no longer matches the plan, or ``None`` if it does.

        The engine calls this immediately before backing up and applying. A
        preview can sit open for minutes; in that time a player may change
        the very setting the plan was computed from. Applying the stale plan
        would then overwrite their change, and backing up the stale "before"
        value would make a rollback restore a state that no longer existed.

        The default re-scans and compares both the current and the desired
        value with what the operator approved. A tweak whose safety is
        re-established per item at apply time may override this.
        """
        fresh = self.scan(ctx)
        if not fresh.needs_change:
            return ALREADY_DESIRED
        if (
            fresh.current_value != planned.current_value
            or fresh.desired_value != planned.desired_value
        ):
            return (
                f"изменилось после превью (было {planned.current_value!r}, "
                f"стало {fresh.current_value!r}); ничего не изменено — "
                "пересканируйте и проверьте заново"
            )
        return None

    @abstractmethod
    def backup(self, ctx: TweakContext, state: TweakState) -> BackupRecord:
        """Capture prior state precisely enough to restore it."""

    @abstractmethod
    def apply(self, ctx: TweakContext, state: TweakState) -> ApplyResult:
        """Perform the change."""

    @abstractmethod
    def verify(self, ctx: TweakContext, state: TweakState) -> Verification:
        """Re-read the system and confirm the change took effect."""

    @abstractmethod
    def rollback(self, ctx: TweakContext, backup: BackupRecord) -> ApplyResult:
        """Restore the state captured in ``backup``."""

    def describe(self) -> dict[str, Any]:
        """Catalogue entry, as required by rule #11."""
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "rationale": self.rationale,
            "risk": self.risk.value,
            "subsystem": self.subsystem,
            "scope": self.scope.value,
            "requires_admin": self.requires_admin,
        }
