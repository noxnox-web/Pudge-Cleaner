# Pudge Cleaner

**v1.0.0-dev.4** — local diagnostics, optimization and configuration management
for gaming-club PCs. Runs entirely offline. No server, no account, no telemetry.

> **Status: core in development.** The foundation, hardware scanner,
> tweak/rollback engine, cleaner, OPTIMIZE pipeline, Golden Profile
> capture/compare, network diagnostics, Steam club-reset and dashboard are
> implemented and tested. Profile *restore* and the maintenance agent are not
> yet built. See [Current state](#current-state).

---

## What it does

An administrator installs PGM on a club PC, picks a profile, and presses one
button. The PC scans itself, reports what is wrong, shows exactly what it
intends to change, applies only what was approved, verifies each change took
effect, and undoes anything that failed.

## What it is not

It is not an "FPS booster". PGM does not disable Defender, does not turn off
Windows Update, and does not apply registry tweaks it cannot justify. Every
change answers six questions before it ships (rule #64):

1. What does it change?
2. Why?
3. What is the expected effect?
4. What are the risks?
5. How is it verified?
6. How is it rolled back?

A change that cannot answer all six is not included.

---

## Safety model

```
SCAN -> DIAGNOSE -> PLAN -> [preview] -> BACKUP -> APPLY -> VERIFY
                                                              |
                                                    failed ->  ROLLBACK
```

Enforced by `TweakEngine`, not by individual tweaks:

- **No change without a durable backup.** The backup row is committed to SQLite
  *before* the mutation. A tweak whose backup fails is skipped, whatever its
  risk level.
- **No unverified success.** After applying, PGM re-reads the setting. If it
  did not hold, the change is rolled back automatically.
- **Success means "changed and verified"** — never "performance improved".
  Performance is measured separately by the benchmark subsystem (rule #65).
- **Risk gating.** `SAFE` and `LOW` may auto-apply. `MEDIUM` and above require
  administrator rights *and* explicit opt-in: the optimization mode on the main
  window, «Обычная» by default and never remembered between launches.
  `CRITICAL` is never auto-applied.
- **Dry run always available**, and always shown before anything mutates. The
  operator can untick any planned change; unticked changes are skipped.
- **No stale plans.** Just before applying, each tweak re-reads the system. If
  the setting changed since the preview, the change is skipped, not forced.
- **Interrupted changes are surfaced.** A change is marked applied only after
  it verifies. If PGM dies mid-change, the next start lists what was left
  unverified, with the original value — it does not roll back unattended.

### Honest reporting

A value PGM cannot read is shown as `UNAVAILABLE` with the reason. It is never
shown as `0`, and never guessed. Thresholds carry their provenance: a
vendor-documented limit and a PGM heuristic do not look alike in the UI.

---

## Requirements

| | |
|---|---|
| OS | Windows 10 (19045+) or Windows 11 x64 |
| Python | 3.12+ (developed and tested on **3.14.7**) |
| Shell | Windows PowerShell 5.1 (present on all supported versions) |
| Privileges | Not required to scan. Required to change system settings. |

Read-only scanning deliberately works without administrator rights.

## Installation (development)

```bash
git clone <repo> pudge-gaming-manager
cd pudge-gaming-manager
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## Running the tests

```bash
.venv/Scripts/python.exe -m pytest
```

The suite includes architecture tests that fail the build if the layering is
violated or if `subprocess` is used outside its one sanctioned module.
Destructive operations are exercised only against fakes — never the live
machine.

## Building the executable

```bash
.venv/Scripts/python.exe -m pip install pyinstaller
.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean PudgeCleaner.spec
```

The result is `dist/PudgeCleaner.exe`, a single self-contained file.
`PudgeCleaner.spec` is the build configuration and is kept in the
repository: it embeds a manifest requesting elevation (`uac_admin`, so the
executable always runs as administrator, which the optimize and Steam-reset
actions need), sets the icon, ships `app/gui/assets` at the same relative
path `app/gui/resources.py` looks in, and — the part a command line would
lose — excludes the Qt modules and plugins a widgets program never loads
(QML/Quick, PDF, OpenGL, Network, Qt's OpenSSL, the 20 MB software OpenGL
fallback) and pywin32's MFC layer. A one-file exe unpacks everything on
every launch, so that trim took it from 58.5 MB to 37.4 MB.

`run.py` is the entry point. `PGM_SELFTEST=1` constructs the dashboard and
exits, which is how a build is smoke-tested without a prompt: it asserts the
bundled logo loads and that WMI works in-process, so a missing asset or a
bundle without the COM bindings fails instead of shipping. It runs
off-screen by default; `QT_QPA_PLATFORM=windows` runs it on the real
platform plugin, the one club PCs use.

---

## Current state

### Implemented and tested

| Component | Notes |
|---|---|
| Exception hierarchy | Every error carries *what / reason / remedy* (rule #36) |
| `CommandRunner` | The only module allowed to use `subprocess`; timeout, capture, OEM-codepage decoding. Bare program names resolve **only** from `System32`/`Windows`, never PATH or the working directory, so a planted `powercfg.exe` cannot run elevated |
| `PowerShellRunner` | PS 5.1, `-EncodedCommand`, env-var parameters (injection-safe), explicit JSON depth |
| `privileges` | Token-based elevation detection, per-operation gating |
| `logging_setup` | `application.log`, `audit.log` (JSON lines), `errors.log` |
| SQLite layer | 14 tables, migrations, WAL, **append-only audit log enforced by trigger**, written for every applied change, rollback and interrupted change. Refuses a data folder or database created first by another account (ProgramData lets any user create one) |
| Hardware scanner | CPU, RAM, GPU, storage, displays. The inventory is read in-process through WMI (pywin32 COM), 0.23 s; one batched PowerShell CIM call (~3 s) remains as the fallback when COM is unavailable |
| Display module | `EnumDisplaySettingsExW` mode enumeration, refresh-rate capability detection, `CDS_TEST` dry run |
| `TweakEngine` | Full lifecycle with all safety invariants |
| Tweak contract | Rationale required at class-definition time |
| `PowerManager` | Locale-independent `powercfg` parsing (GUID + `*` marker) |
| `SetPowerPlanTweak` | Switches the active scheme to High Performance; **verified apply/rollback on real hardware**. Offered by `OPTIMIZE PC`, and self-skips when that scheme is already active or absent |
| `SetRefreshRateTweak` | Raises a display to its maximum refresh rate at the current resolution |
| Cleanup engine | Allowlisted cleanup across 20 categories — temp files, shader caches, crash dumps, servicing logs, six browsers' page caches (per-profile, via wildcard roots), five game launchers' caches and logs, and the Downloads folder — plus the Recycle Bin through `SHEmptyRecycleBinW`. Five safety layers, two narrow and individually-tested waivers; counted in the preview before anything is deleted and marked irreversible in the UI |
| Scan versus refresh | A full scan is two PowerShell calls — the hardware inventory (~2.3 s) and the routing query (~2.9 s, run beside it) — and it is what the window opens with and what the rescan button asks for. After an action the dashboard *refreshes* instead: every live value is read again (processor load, GPU temperature, free space, display mode) while the hardware inventory is reused and the previous network reading is carried forward untouched. 80 ms against 3.5 s, and nothing carried over is shown as freshly measured |
| Gaming tweaks | Display refresh rate, temporary-file cleanup, Recycle Bin, power scheme (refused on dual-CCD Ryzen X3D, which needs Balanced for AMD's core parking), USB selective suspend, PCIe link power, pointer acceleration, Game DVR (per-user and policy), `Windows.old`, and the catalogue's recommended tier — windowed-game optimisations (Windows 11), transparency, window animations. Each names a documented mechanism, self-skips when already correct, and is verified by re-reading the machine rather than by trusting the write |
| «Настройки Windows» | A catalogue (`core/settings/catalog.py`) of registry-backed settings in three tiers. Every row shows the current value, the recommendation — or that there is none and why — where the sources disagree, the risk, when it takes effect, the minimum build and hardware. Nothing is pre-selected. Game Mode and HAGS (with support read from the display kernel, `D3DKMT_WDDM_2_7_CAPS`) are *advanced*; NetworkThrottlingIndex, SystemResponsiveness, MMCSS «Games» Scheduling Category and Win32PrioritySeparation are *experimental* and in no preset. Also here: USB and network-adapter power saving and Energy-Efficient Ethernet, each backing up every device's previous state |
| System audit | Read-only report: CPU topology (SMT, P/E cores, cache domains), GPU driver, monitors, disks, every catalogue setting, power scheme, pointer acceleration, which USB controller each mouse and keyboard is on, network driver age and advanced properties, MSI mode, HVCI / Secure Boot / TPM / Defender / Windows Update, autostart count, the NVIDIA / AMD control-panel checklist, and a list of what the program deliberately does not do, with reasons (`core/audit/policy.py`) |
| Handle-based tree deletion | `utilities/tree_delete.py`, used for `Windows.old`, Steam games and Steam caches. Every child is opened with `NtOpenFile` *relative to its already-open parent* and with `FILE_OPEN_REPARSE_POINT`, so a folder swapped for a junction mid-delete is removed as a link and never followed (the race behind Rust's CVE-2022-21658); directories are enumerated through their handle. `Windows.old` is deleted with backup intent — SeBackup/SeRestore enabled for the duration — instead of `takeown`/`icacls`, which were found to walk into junctions and could re-permission the live `C:\Users` and `C:\ProgramData`. 20 000 ACL-locked files: 25 s → 1.7 s; its size is measured the same way, 10× faster than `stat` per file |
| Change history and undo | Every reversible change, from any run, can be undone later — one at a time or all, newest first — through the tweak's own verified rollback. Undoing an older change beneath a newer one is refused. A Windows restore point is attempted before each real run; it is checked by sequence number, and its failure never blocks the run |
| Load recording | Samples CPU (total and busiest core), RAM, and on NVIDIA GPU load, temperature, power, clock and throttle reasons while someone plays. It does **not** measure FPS or frame time — that needs ETW present events — and says so instead of estimating |
| Application removal | Xbox apps, Widgets, consumer Teams, OneDrive and a named list of preinstalled Store apps. Three guards, because this is the one thing the program cannot undo: an explicit catalogue with no pattern matching, `System`-signed packages refused as OS components, and every deliberate omission recorded with its reason — `Microsoft.XboxIdentityProvider` above all, because removing it breaks Game Pass, Minecraft, Forza and Xbox-Live Steam titles on the machine the tool exists to make better at games |
| Experimental: blocking dota2.com | Adds `dota2.com` and `www.dota2.com` to a PGM-owned block in the hosts file, on one unreplicated report in Valve's tracker ([Dota2-Gameplay #35438](https://github.com/ValveSoftware/Dota2-Gameplay/issues/35438)) that it stops Dota 2's frame rate decaying across a long session. No documented mechanism, no reproduction, no Valve response, and an unruled-out confound — applying it means restarting the client, so every "blocked" measurement began on a fresh process. MEDIUM and unticked; the rationale states the evidence and that the in-client news, event and store panels stop loading. The block is delimited, so other tools' entries are never touched and removal restores the file byte for byte |
| Optimization mode | MEDIUM tweaks used to be dropped by the risk gate before the operator saw them, and later could only be reached by re-planning from the preview. The mode is now a three-way switch above OPTIMIZE, chosen before planning: «Обычная» plans SAFE and LOW only; «Средняя» plans MEDIUM too but delivers it **unticked**, so allowing the class and choosing a change stay two separate acts; «Жёсткая» delivers every row ticked, irreversible removals included — choosing that mode is the operator's choice of the whole class, the hint under the switch says so in the warning colour, and the preview still names every row and lets any of them be unticked. Every session starts at «Обычная» |
| Cleanup performance | A full pass over 20 000 files went from 43 s to under 5 s, and the delete step alone from 2.4 s to 1.0 s once files were opened with `DELETE` and `READ_ATTRIBUTES` only: asking for read access, as before, made Defender scan each file on open before deleting it (818 µs against 87 µs per file). The delete-time gate resolves each parent directory once instead of each file, categories are scanned in parallel, deletions are overlapped across eight threads, and `verify` reads free space instead of walking the tree a second time. The largest single win was replacing `pathlib.is_relative_to`, which builds and compares every ancestor Path on this Python — 15 us per call, several calls per file |
| Disk usage survey | Reports the largest folders at a fixed depth under the profile, ProgramData, both Program Files and Windows, with a per-root time budget and an explicit "partial" flag. From the list a folder can be **deleted** or **moved to another drive** (below) — the survey itself still deletes nothing: it exists to answer what the cleaner's allowlist deliberately says nothing about |
| Startup manager | Lists `Run`/`RunOnce` entries (HKCU, HKLM, 32-bit view) and both Startup folders with their enabled state, and toggles them through the `StartupApproved` flag Task Manager writes. Nothing is deleted, the change is reversible, and the state is read back from the registry rather than assumed. PGM never writes to a `Run` key — the allowlist still refuses them |
| `OptimizationPipeline` | Scan findings -> tweaks -> preview -> apply -> rescan, with a before/after report |
| `RegistryManager` / `ServiceManager` | Allowlist matched per path segment and per hive; `Services` and `Run` keys deliberately excluded. Type-preserving backups, protected-service list. Not yet used by any tweak |
| Network diagnostics | Physical uplink (cable/Wi-Fi, negotiated speed, duplex) kept apart from the path Windows actually routes through, so a VPN is reported as a VPN and never as the network card. Router and internet latency, loss and jitter via `IcmpSendEcho` — no admin rights, no parsing of translated `ping.exe` output. Runs beside the hardware scan |
| Golden Profile | Capture this PC, compare another against it, load/save as JSON. Loading is a trust boundary: size-limited, unknown fields rejected. Capture also records the installed Steam games as the club-reset keep-list and the «Настройки Windows» values (catalogue ids and option keys only — no paths, no raw values). Drift in that section, in the power scheme and in display refresh rate can be corrected from the comparison window («Привести к профилю…»), through the normal plan, confirmation, backup and verification; hardware drift is reported only |
| Steam club reset | As SteamWiper: removes every game except a built-in keep-list of popular titles (edit `games/steam/default_keep.py`; no profile or config file), with a preview whose checkboxes let the operator rescue any game before deletion; clears `downloading`, `temp`, `shadercache`, `workshop` (whole — including kept games' mods/maps) and `sourcemods` in every library, plus `appcache`, `logs`, `dumps`, `userdata`; **signs every account out** — empties `config` (keeping `config.vdf` and `libraryfolders.vdf`) and deletes each Windows user's saved tokens (`local.vdf`) and Steam web cookies (`htmlcache`). Preview-first with sizes and the number of remembered accounts; everything deleted by handle (junction-swap safe); Steam stopped first |
| Gaming Score | Transparent, weights configurable, unavailable inputs excluded and renormalised |
| Issue detection | Findings carry severity, remedy and threshold provenance |
| Dashboard GUI | PySide6 dark theme; scan, optimize, profile, cleanup and Steam-reset work run on worker threads; score explainer; one primary action (**Оптимизировать ПК**) with every other tool grouped beside the metrics (Диагностика / Настройка / Профиль клуба / Очистка). Lime accent from the club logo, text contrast ≥ 4.5:1, a keyboard-only focus ring, fits a 1024 px-wide screen. Optimize, disk cleanup, the Steam reset and the usage survey show one bar under the status line — filled when the step can say how far it is, animated when it cannot. A write in progress (apply, wipe, cleanup, or a folder delete or move) blocks the window from closing |

**914 tests passing**, plus one opt-in live test that changes and restores
the active power plan (`PGM_LIVE_SYSTEM_TESTS=1`).

### Running it

```bash
.venv/Scripts/python.exe -m pudge_gaming_manager
```

### Not yet built

Process analyzer · DNS diagnostics · Windows repair (SFC/DISM) ·
Self-healing · Per-game tuning profiles · Session mode · FPS / frame-time capture ·
Maintenance agent · Installer

**`OPTIMIZE PC`** covers the display's refresh rate, disk cleanup and the
Recycle Bin, the power scheme and two settings inside it, pointer
acceleration, Game DVR's background recording, the experimental dota2.com
hosts block, the catalogue's recommended settings, the `Windows.old`
upgrade-rollback folder, and the removal of Xbox apps, Widgets, consumer
Teams, OneDrive and a named list of preinstalled Store apps. HAGS, Game Mode
and the scheduler values are not in it: the sources disagree on them, so
they are chosen by a person in «Настройки Windows».

The removals are the only tweaks that cannot be undone, so each is marked
НЕОБРАТИМО on its own row in the preview, each is MEDIUM — held behind the
optimization mode, and arriving unticked unless «Жёсткая» was chosen — and
none of them matches by pattern. Nothing
is removed that is not named in `windows/apps/catalogue.py`, no `System`-
signed package is removed at all, and every name deliberately left out is
recorded there with the reason.
Each one self-skips when it is already correct or inapplicable, and the
preview says so rather than omitting the row.

**`Очистка диска…`** is the cleaner on its own. Unlike the cleanup inside
`OPTIMIZE PC`, which runs the default categories, this scans *every*
category plus the Recycle Bin and lets the operator tick what goes. Two
categories start unticked: the Windows servicing logs, which are wanted when
a failed update is being diagnosed, and the Downloads folder, which holds
the user's own files. Browser and launcher caches are on by default — they
name cache and log directories only, so nobody is signed out and no game is
touched.

The result reports **two** numbers, because they are two different facts:
the logical size of everything deleted, and how much free space the drives
actually gained, read before and after. Shadow copies, deduplication and
cluster rounding make them differ, and when the gap is large the report says
which of those is the likely cause instead of quietly showing the flattering
number.

**`Что занимает место…`** lists the largest folders. The cleaner works from an
allowlist so it can never remove something nobody listed; the price is that it
says nothing about the 200 GB in a folder no category names. This answers
that, and now lets the operator act on the answer: select a folder, then
**Удалить** or **Перенести на другой диск**. Both ask first, naming the folder,
its size and that nothing goes to the Recycle Bin, with Cancel as the default.

What may be touched is decided by `windows/folder_policy.py`, not by the
cleaner's protected-name table, which is written for unattended cleanup and
closes Program Files, `Documents` and every launcher — the folders a person
most wants to move. The policy refuses the Windows folder, the profile and
Program Files as a whole, Windows and Microsoft components (`WindowsApps`,
`Common Files`, `ProgramData\Microsoft`, `AppData\...\Microsoft`, `Packages`,
`Temp`), links, and club software, anti-cheat and credentials. It is a
denylist, so it is never complete; it covers what cannot be put back.

A move is a copy, a swap and a delete, because there is no atomic move
between drives:

1. Rename the folder away and back. Anything open inside makes the rename
   fail (verified on this machine: an open file, another process's working
   directory and a held directory handle all do), so this finds a folder in
   use in a moment instead of after copying 100 GB.
2. Copy with `CopyFileExW`, which keeps attributes, times and alternate data
   streams and reports bytes as it goes. Paths over 260 characters work. A
   link anywhere inside stops the move before it starts; an EFS-encrypted file
   fails rather than being written out decrypted.
3. Measure the source again and the copy: a program that wrote into the folder
   meanwhile shows as a difference, and the move is abandoned.
4. Rename the original aside, make a junction at the old path, delete the
   renamed original. The junction is written directly with
   `FSCTL_SET_REPARSE_POINT`; `mklink /J` would pass the path through `cmd`'s
   parser, which expands `%NAME%` even in quotes.

Until step 4 the original is untouched, and a failure anywhere before it
removes the partial copy. If the junction cannot be made the original is put
back; if it cannot be put back, both copies are kept and the message names
where each is. Files that cannot be deleted afterwards are reported with the
folder that holds them. Deleting uses the handle-based delete without backup
privileges, so an ACL that denies the file still counts: denied files are
reported, not forced. The copy names files by path, not by handle, so
someone who can write inside the source could swap a folder for a link
between listing and opening it; each entry is checked as it is reached, and
the source is deleted by handle.

**`Автозагрузка…`** lists what Windows launches at sign-in and turns entries
on or off the way Task Manager does — by writing the `StartupApproved` flag,
never by deleting the entry. Disabling is reversible, and Task Manager shows
the same state afterwards.

The profile comparison checks hardware, power plan, display, «Настройки
Windows» and service startup types. Services are report-only — there is no
tweak that changes a startup type, protected services are refused by the
schema, and automatic versus delayed-automatic compare as equal because
`Get-Service` on PowerShell 5.1 cannot tell them apart. The `cleanup`
section is a club policy rather than machine state, so it is validated and
stored but has nothing to compare.

---

## Known limitations

These are stated rather than worked around. Each is a deliberate scope
decision, not an oversight.

**GPU telemetry is NVIDIA-only.** On NVIDIA cards temperature, load, VRAM,
power, clocks and throttle reasons are read through NVML, NVIDIA's documented
read-only library — on the dashboard and in the load recording. AMD and Intel
cards report identity (model, driver) only: no equivalent documented
interface ships with Windows.

**NVIDIA and AMD control-panel settings are not changed.** Writing them has no
public documented API — NVIDIA's needs `NvAPI_DRS_*` with reverse-engineered
setting IDs. The system audit lists them as a manual checklist instead.

**`Win32_VideoController.AdapterRAM` is not used.** It is a 32-bit field and
reports a 12 GB RTX 3060 as 4 GB. VRAM comes from NVML, with a one-shot
`nvidia-smi` query as the fallback when NVML cannot load; on AMD/Intel GPUs
VRAM is reported as unavailable.

**CPU temperature is not available.** Windows exposes no reliable general CPU
thermal sensor. `MSAcpi_ThermalZoneTemperature` reports an ACPI zone that is
often the motherboard and frequently wrong on desktops. Reporting it as "CPU
temperature" would be a fabrication.

**SSD temperature and raw SMART attributes are not read.** These need a
privileged raw device handle and vendor-specific decoding. Health comes from
`MSFT_PhysicalDisk.HealthStatus` instead, which is documented and reliable.

**Internet latency is measured to a reference point, not a game server.**
The reference is `1.1.1.1`. Latency to a particular game depends on where
that game's servers are; PGM does not claim otherwise.

**Latency through a VPN or tunnel is often not measurable.** Tunnel
software commonly answers ping itself. Before measuring, PGM sends one echo
to `192.0.2.1` (TEST-NET-1, RFC 5737), which is routed nowhere; if it
answers, replies on that path are fabricated, and PGM reports the latency as
not measurable instead of showing a 0 ms that never happened.

**Round-trip times have 1 ms resolution** (`IcmpSendEcho` reports whole
milliseconds), so a LAN hop reads `<1 ms`. Network diagnostics are IPv4
only. A router that ignores ping is treated as filtering ICMP, not as a
broken LAN. When traffic to the router itself is routed through a VPN, the
router's latency is reported as not measurable. A connected card with no
default gateway (a LAN-only event) is reported as "no internet route", not
as disconnected.

**HDR and VRR/G-SYNC state are not detected.** Not exposed by the Win32
display API; would require vendor libraries.

### Unverified on the target platform

The gaming tweaks were exercised on one machine: an Intel desktop with an
NVIDIA card, a single monitor, Windows 10, and Ultimate Performance active.
Its Store apps had already been stripped, so the application-removal tweaks
found nothing to do: their catalogue, guards and refusal paths are covered
by tests with a faked inventory, and no real uninstall was performed on any
machine. USB selective suspend was applied, verified and rolled back for
real on it;
pointer acceleration and Game DVR's per-user switch were already at their
target there, so their scan and
rollback paths are covered by tests rather than by observation. AMD
hardware, laptops and multi-monitor setups are untested.

Launcher and browser cache categories are written from vendor layout rather
than from observation on every product. On the development machine the Epic,
Riot and EA categories were confirmed to find real files, and Chrome and Edge
likewise; Battle.net, Ubisoft Connect, Opera, Firefox, Yandex and Brave were
not installed and report "на этом ПК нет". A category whose layout differs
finds nothing — it cannot delete the wrong thing, only come up empty.

The development machine is **Windows 10 Pro 19045** with no club software
installed. The following are implemented from documentation and have **not**
been executed in their target environment:

- `OPTIMIZE PC` **apply** from the GUI on a machine that needs changes. The
  preview path has run live; the dev machine had no findings to apply.
- Network findings for Wi-Fi, sub-gigabit links, half duplex, packet loss
  and a disconnected PC. The dev machine is on healthy gigabit Ethernet
  behind a VPN, so only the VPN detection and the fabricated-reply check
  have fired live; the rest are covered by unit tests.
- The Steam reset **removing** games from the GUI. Detection, enumeration
  (12 real games across two libraries), sizing and the dry-run preview have
  run live on this machine; the actual deletion is covered by tests against a
  fake Steam tree (including a junction-swap attempt), never the real install.

- Delete and move from the usage dialog, through the buttons. The same code
  has run live here: a 303 MB, 1 501-file folder moved from `C:` to `D:`
  (every file identical by SHA-256 when read back through the junction), then
  deleted, on throwaway data. Clicking through the GUI and moving a real
  application's folder have not been done.
- Every Windows 11-specific code path.
- SmartShell protection — no SmartShell installation exists here. The
  protected-application list is config-driven and untested against the real
  product.
- Multi-GPU and AMD/Intel-only machines. Only a single NVIDIA RTX 3060 was
  available.
- A monitor running below its maximum refresh rate. Both attached displays
  already run at their maximum (180 Hz and 240 Hz), so the detection logic is
  covered by unit tests with constructed values rather than live hardware.

---

## Documentation

| File | Contents |
|---|---|
| `ARCHITECTURE.md` | Layering, change lifecycle, rollback, privilege and security model |
| `README.md` | This file |
| `LICENSE` | MIT |

Planned: `SECURITY.md`, `TWEAKS.md`, `PROFILE_SCHEMA.md`, `DEVELOPMENT.md`,
`CHANGELOG.md`.

---

## Privacy

PGM does not collect personal documents, file contents, passwords, cookies,
messages or game credentials. It stores no secret of any kind, which is the
cheapest way to never leak one. All data stays in
`%ProgramData%\PudgeGamingManager\`. The folder keeps its original name after the rename to Pudge Cleaner: moving it would orphan the database, backups and audit log on every machine already running it.

---

## License

MIT — see [`LICENSE`](LICENSE). Use it, change it, ship it; keep the copyright
notice, and understand that it comes with no warranty. That last part is not
boilerplate here: this program deletes files and changes Windows settings on
machines it does not own. Read what a button does before you press it on
someone else's PC.
