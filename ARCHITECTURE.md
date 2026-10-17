# Pudge Cleaner — Architecture

**Version:** 1.0.0-dev · **Status:** Core in development · **Date:** 2026-09-20

---

## 1. Purpose and scope

Pudge Cleaner (PGM) brings a gaming-club PC to a known-good reference
configuration, verifies that it stayed there, and reverses any change it made.

It is **not** an "FPS booster". Every change is documented, reversible,
risk-rated and verified. A change PGM cannot describe, undo and verify is a
change PGM does not make.

**Non-goals for v1.0:** cloud backend, user accounts, telemetry upload,
automatic driver installation, overclocking, anti-cheat interaction.

---

## 2. Target and verification reality

| | Target | Development machine | Consequence |
|---|---|---|---|
| OS | Windows 11 x64 | **Windows 10 Pro 19045** | Win11-only branches are written from Microsoft documentation and marked `@unverified_on_win11`. They are not proven. |
| Shell | — | **PowerShell 5.1** (no PS7) | All emitted script must be 5.1-compatible. No `??`, no `?.`, no ternary, no `-AsHashtable`. |
| Python | 3.12+ | **3.14.7**, single interpreter | `PySide6 6.11.2` resolves as compatible. Pinned in `requirements.txt`. |
| Club software | SmartShell present | **absent** | Protected-application handling is config-driven and **cannot be verified here**. |
| GPU | NVIDIA | RTX 3060 present | See §7. |

**Verification honesty rule.** Code paths that could not be executed on the
development machine carry an `@unverified` marker naming what was not proven.
`README.md` carries the same list. PGM never reports a configuration as
verified when the verification step itself did not run.

---

## 3. Layering

Dependencies point downward only. A layer never imports from a layer above it.

```
      app/          GUI (PySide6), controllers, view-state
        │           Knows nothing about the registry, services or powercfg.
        ▼
      core/         Orchestration: scan -> diagnose -> plan -> apply -> verify
        │           Owns TweakEngine, RollbackEngine, scoring, profiles.
        ▼
  windows/ network/ hardware/ games/            Subsystems
        │           Each exposes a narrow interface. No cross-imports.
        ▼
   utilities/       CommandRunner, PowerShellRunner, RegistryManager,
                    ServiceManager, privileges, logging, exceptions
        ▼
   database/        SQLite persistence
```

**Enforced by test.** `tests/test_architecture.py` walks the import graph and
fails the build on an upward or sideways import. The rule is executable, not
advisory.

### Why the GUI is fully isolated

`app/` may only call `core/`. It cannot import `winreg`, call `subprocess`, or
touch `windows/`. This is what makes rule #12 ("never change the registry from
the GUI") a structural property rather than a convention someone remembers.

---

## 4. The change lifecycle

Every system modification is a **Tweak** implementing one interface:

```python
class Tweak(Protocol):
    id: str                  # stable, versioned: "power.plan.high_performance@1"
    risk: RiskLevel          # SAFE | LOW | MEDIUM | HIGH | CRITICAL
    rationale: str           # why this helps; cited where a source exists

    def scan(self, ctx) -> TweakState:      # observe, never mutate
    def validate(self, ctx) -> Validation:  # is applying it safe and possible?
    def backup(self, ctx) -> BackupRecord:  # capture exact prior state
    def apply(self, ctx) -> ApplyResult:    # mutate
    def verify(self, ctx) -> Verification:  # re-read; did it actually take?
    def rollback(self, ctx, backup) -> RollbackResult
```

`TweakEngine` drives the sequence and is the **only** component permitted to
mutate system state:

```
scan -> validate -> backup -> apply -> verify -+- ok     -> commit
                                               +- failed -> rollback
```

Invariants the engine enforces, not the individual tweak:

- `apply` never runs without a durable `BackupRecord` already written to SQLite.
- A failed `verify` triggers `rollback` automatically.
- A tweak that cannot `backup` cannot `apply`, regardless of risk level.
- `SAFE` and `LOW` may auto-apply. `MEDIUM`+ requires admin **and** an
  explicitly enabled policy — never a default.
- Dry-run short-circuits after `validate`, producing the plan with no mutation.

### Success means "changed and verified"

`ApplyResult.SUCCESS` asserts that the configuration changed and re-reading
confirmed it. It asserts **nothing** about frame rate. Performance claims come
only from the benchmark subsystem, measured separately (rule #65).

---

## 5. Rollback

Every backup is a row in SQLite, written **before** mutation and surviving a
crash or power loss mid-apply:

```
backups(id, tweak_id, tweak_version, run_id, timestamp,
        scope, key, old_value, old_value_absent, new_value, state)
```

`old_value_absent` distinguishes "the registry value was empty" from "the value
did not exist" — restoring the wrong one of those leaves the system in a state
it was never in. This is the single most common rollback bug in tools of this
kind, and the schema makes it unrepresentable.

A backup moves out of `APPLYING` only once its change is verified (`APPLIED`)
or undone (`RESTORED` / `FAILED`). On startup the dashboard lists every backup
still in `APPLYING` — an interrupted change — with its original value, then
marks it reported. It does **not** roll back unattended: an unpreviewed change
is exactly what the safety model forbids, and the operator may have fixed the
setting by hand since.

Implemented granularity: a single tweak, rolled back automatically when it
fails to apply or verify. Whole-run rollback and named snapshots are not built.

---

## 6. Privilege boundary

Two processes, deliberately:

| Process | Privilege | Responsibility |
|---|---|---|
| `PudgeGamingManager.exe` (GUI) | **user**, elevates per-operation | Presentation, planning, read-only scanning |
| `PudgeGamingManagerAgent.exe` | service | Scheduled maintenance, drift detection |

The GUI does not run elevated as a whole (rule #37). It detects its own token,
states plainly which operations need elevation, and requests it only for those.
A read-only scan must never require admin — the most common operation carries
the smallest privilege.

**Nothing executable crosses a configuration boundary.** Profile JSON may
contain values and documented enum keys only. It may not contain commands,
paths-to-execute, script, or registry paths outside a hardcoded allowlist.
Schema validation rejects the file before a single value is read (rule #56).
A profile from a USB stick is untrusted input and is treated as such.

---

## 7. NVIDIA — declared scope

**v1.0 is detection-only, by explicit decision.**

There is no public, documented NVIDIA API for *writing* control-panel settings.
The options are NVML (documented, read-only) and `NvAPI_DRS_*` (writes profiles,
but thinly documented and requiring reverse-engineered setting IDs). Rules #14
and #63 prefer documented interfaces, so v1.0 ships neither profile writing nor
sensor polling.

| Capability | v1.0 | Source |
|---|---|---|
| GPU present, model, driver version/date | **yes** | WMI `Win32_VideoController` |
| True VRAM size | **yes** | one-shot `nvidia-smi` query |
| GPU temperature, utilization, clocks, power | **no** | would require NVML polling |
| Per-game profile read/write | **no** | would require NVAPI DRS |

`Win32_VideoController.AdapterRAM` is a 32-bit field and reports this machine's
12 GB RTX 3060 as 4 GB. PGM does not read it. This is why VRAM comes from
`nvidia-smi` instead.

Consequences, stated rather than hidden: Hardware Health renders GPU thermal and
utilization rows as `UNAVAILABLE`, never as a guess. The GPU component of the
Gaming Score grades driver currency and capability, not thermals, and the score
breakdown says so on screen. The boundary is drawn at **one-shot static facts
during a scan (detection) vs. continuous sensor reads (telemetry)**.

**Where the code lives.** Because v1.0 NVIDIA support *is* detection, GPU
detection sits in `hardware/gpu/`, not in a separate `nvidia/` layer: model,
driver and VRAM are hardware facts shared with every other probe. A dedicated
NVIDIA layer returns only when the deferred NVAPI profile work lands, which
would add NVIDIA-specific *behaviour* rather than shared vocabulary. The
first attempt put detection in `nvidia/` and the import-graph test rejected
it as a sibling dependency — the rule in §3 doing its job.

---

## 8. Thresholds and scoring

Every threshold is one of three kinds, and the kind is stored with the value:

| Kind | Meaning | Shown as |
|---|---|---|
| `VENDOR` | From vendor/Microsoft documentation, cited | the cited limit |
| `HEURISTIC` | PGM's own judgement | labelled "heuristic" in the UI |
| `DERIVED` | Computed from observed hardware | shows its derivation |

A `HEURISTIC` threshold is never presented with the authority of a `VENDOR`
one (rule #8).

Gaming Score weights live in configuration, default GPU 25 / CPU 20 / Network 15
/ RAM 10 / Storage 10 / Display 10 / System 10. The UI shows the formula, each
component's input, and every input that was `UNAVAILABLE`. A component with no
available input is excluded and the weights renormalize — it is never silently
scored as zero, which would punish a PC for PGM's own blind spot.

The score is a diagnostic indicator. It is not FPS and the UI does not imply it is.

---

## 9. Subsystem command discipline

`subprocess.run` appears in exactly one module: `utilities/command_runner.py`.
The architecture test fails the build if it appears anywhere else (rule #49).

`CommandRunner` guarantees timeout, captured stdout/stderr, exit code,
structured error, audit log entry, and no `shell=True` unless a caller passes an
explicit, reviewed opt-in. `PowerShellRunner` builds on it, targets 5.1,
and passes arguments via a parameter block rather than string interpolation.

Errors surface as cause and remedy, never as a raw traceback (rule #36):

```
Failed to modify Windows service "Spooler".
Reason:   Access denied.
Required: Run Pudge Cleaner as Administrator.
```

---

## 10. Self-healing and drift

`SelfHealing` runs detect -> diagnose -> repair -> verify, capped at **3 attempts**
per issue per run, then reports `MANUAL ACTION REQUIRED`. An issue that failed
three times is recorded and not retried on the next run until its input state
changes — this prevents a permanently-broken item from consuming every
maintenance window forever.

Drift detection compares the live scan against the Golden Profile and reports
differences. It never auto-corrects; `RESTORE GOLDEN PROFILE` is a human action.

---

## 11. Data flow — OPTIMIZE PC

```
 Scan --> Diagnose --> Plan --> [DRY RUN preview, always shown]
                                      |
                            user confirms scope
                                      v
                    per tweak: backup -> apply -> verify
                                      |
                         any failure -+-> rollback that tweak
                                      v
                          Before/After report + audit log
```

The dry-run preview is not optional and not skippable. The user sees the
complete list of planned changes, with risk levels, before anything mutates.

---

## 12. Storage

SQLite at `%ProgramData%\PudgeGamingManager\pgm.db`, WAL mode.

`system_info · scan_results · optimization_runs · tweak_applications · backups ·
profiles · games · maintenance_jobs · benchmark_results · errors · audit_log`

`audit_log` is append-only, holds every privileged operation, and is never
rewritten by the application. No secrets are stored in any table; PGM has no
credential to store, which is the cheapest way to not leak one.

Schema version is tracked in `schema_migrations`; profiles carry an independent
`schema_version` so a v1.0 profile remains readable by a later build.

---

## 13. Protected applications

Club infrastructure is never touched by cleanup, debloat, startup or service
policies:

`SmartShell · Steam · Riot Client · Battle.net · Epic Games · anti-cheat
services (Vanguard, EAC, BattlEye) · club agent · POS software`

Matching is by service name, executable path and publisher — three independent
signals, because any one alone is spoofable or fragile. The list is
user-extensible and ships non-empty. An item on it is excluded from every
destructive operation, with no override flag in v1.0.

**Untested here:** no SmartShell installation exists on the development machine.
This protection is implemented from its documented install paths and service
names, and is `@unverified`.

---

## 14. Testing

| Layer | Approach |
|---|---|
| `utilities/`, `core/` | Unit tests against fakes |
| `windows/`, `nvidia/` | A `SystemInterface` seam; tests inject a fake |
| Rollback | Property test: apply->rollback restores the exact prior state, including the absent-vs-empty distinction |
| Architecture | Import-graph test, `subprocess` containment test |
| Destructive paths | Fakes only — **never** executed against the live machine |

The cleanup engine is tested against a temporary directory tree, never against
real system paths.

---

## 15. Module map

```
pudge_gaming_manager/
├── app/           gui/ · controllers/ · state/
├── core/          scanner/ · diagnostics/ · optimization/ · repair/
│                  rollback/ · scoring/ · profiles/ · gpu_affinity/
├── windows/       services/ · registry/ · power/ · cleanup/
│                  startup/ · processes/ · repair/ · schedule/
│                  (GPU detection lives in hardware/gpu — see §7)
├── network/       diagnostics/ · adapter/ · dns/ · latency/
├── hardware/      probes · storage · gpu/ · monitor/ · models
├── games/         steam/ · cs2/ · dota2/ · valorant/ · generic/
├── database/      schema · migrations · repositories
├── utilities/     command_runner · powershell_runner · registry_manager
│                  service_manager · privileges · logging · exceptions
│                  low_impact · single_instance
├── vendor/        gpu_affinity/ — third-party benchmark binaries + licences
└── tests/
```

Target module size ~300 lines. No file over 500 without a stated reason.

---

## 16. Build order

1. `utilities/` + `database/` + exception hierarchy + logging <- foundation
2. `hardware/` scanner (read-only, no privilege, immediately useful)
3. `TweakEngine` + `RollbackEngine` <- the safety core, before any real tweak
4. First real tweaks: power plan, monitor refresh rate
5. Cleanup engine (against a fake tree first)
6. Dashboard GUI
7. Profiles, Golden Profile, diff, normalization
8. Network, games, self-healing, benchmark, maintenance agent

Nothing that mutates the system is written before step 3 exists and passes its
tests. The rollback path is built before the thing it rolls back.
