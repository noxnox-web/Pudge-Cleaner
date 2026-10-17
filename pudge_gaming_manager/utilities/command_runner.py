"""The single place in Pudge Cleaner permitted to run external commands.

Rule #49: ``subprocess`` must not be scattered across the project. Every
external invocation goes through :class:`CommandRunner`, which guarantees a
timeout, captured output, a structured error, and an audit trail.
``tests/test_architecture.py`` fails the build if ``subprocess`` is imported
anywhere else.

Encoding note
-------------
Legacy Windows console programs (``powercfg``, ``sfc``, ``ipconfig``) write in
the OEM codepage, not UTF-8 — on a Russian-locale machine that is cp866.
Decoding those bytes as UTF-8 silently corrupts every non-ASCII value, which
would then be parsed into settings. The runner therefore captures *bytes* and
decodes through the OEM codepage reported by Windows itself.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess  # noqa: S404 - the one sanctioned import site (rule #49)
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .exceptions import (
    CommandFailedError,
    CommandNotFoundError,
    CommandTimeoutError,
)
from .privileges import require_admin

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0

# Windows creation flag: run without flashing a console window. The GUI must
# never make a black box appear on a club PC mid-session.
_CREATE_NO_WINDOW = 0x08000000


def _trusted_dirs() -> list[str]:
    """Directories a bare program name may be found in.

    Only locations a standard user cannot write to. PATH and the current
    directory are deliberately excluded: PGM runs elevated, and Windows
    searches the current directory first, so a ``powercfg.exe`` dropped
    next to a shortcut would otherwise run as Administrator.
    """
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return [os.path.join(root, "System32"), root]


def resolve_executable(program: str) -> str | None:
    """Turn a program into an absolute path, or ``None`` if it is not found.

    A path (anything with a directory part) is used as given, since the
    caller chose it. A bare name is looked up in :func:`_trusted_dirs`
    only, with ``.exe`` implied, and never through PATH or the working
    directory. Off Windows, bare names still go through ``shutil.which``.
    """
    if os.path.dirname(program):
        return program if os.path.isfile(program) else None
    if os.name != "nt":
        return shutil.which(program)
    name = program if os.path.splitext(program)[1] else program + ".exe"
    for directory in _trusted_dirs():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _oem_encoding() -> str:
    """Return the console OEM codepage, e.g. ``'cp866'`` on Russian Windows.

    Falls back to UTF-8 off-Windows or when the call is unavailable.
    """
    if os.name != "nt":
        return "utf-8"
    try:
        codepage = int(ctypes.windll.kernel32.GetOEMCP())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return "utf-8"
    if codepage == 65001:
        return "utf-8"
    return f"cp{codepage}"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The complete outcome of one external command."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        """True when the command reported success."""
        return self.exit_code == 0

    @property
    def display_command(self) -> str:
        """The command as a readable single line, for logs and errors."""
        return " ".join(self.command)

    def lines(self) -> list[str]:
        """Non-empty stdout lines, stripped. The common parsing entry point."""
        return [ln.strip() for ln in self.stdout.splitlines() if ln.strip()]


class SpawnedProcess:
    """A program started and left running; whoever started it must stop it.

    For the one kind of child ``run`` cannot serve: a benchmark scene that
    has to stay up while another tool measures it. Output is discarded and
    no console window appears.
    """

    def __init__(self, popen: "subprocess.Popen[bytes]") -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def running(self) -> bool:
        return self._popen.poll() is None

    def stop(self, timeout_s: float = 5.0) -> None:
        """End the process, forcefully if it ignores the request. Idempotent."""
        if self._popen.poll() is not None:
            return
        self._popen.terminate()
        try:
            self._popen.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._popen.kill()
            self._popen.wait(timeout=timeout_s)


@dataclass(slots=True)
class CommandRunner:
    """Runs external programs with timeout, capture, logging and typed errors.

    Args:
        default_timeout_s: Applied when a call does not specify one. No command
            ever runs unbounded — a hung tool would otherwise freeze a scan.
        encoding: Output codepage. Defaults to the Windows OEM codepage.
        audit: Optional sink called with a structured record of every command.
            Not wired in production: every command is already logged to
            ``application.log``, and system *changes* are audited by the tweak
            engine into ``audit.log`` and the ``audit_log`` table.
    """

    default_timeout_s: float = DEFAULT_TIMEOUT_S
    encoding: str = field(default_factory=_oem_encoding)
    audit: Any = None

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        check: bool = False,
        requires_admin: bool = False,
        operation: str | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        remedy: str | None = None,
        encoding: str | None = None,
    ) -> CommandResult:
        """Run a command and return its result.

        Args:
            args: Program and arguments as a list. Never a single string —
                arguments are passed to the OS individually, so there is no
                shell to quote for and no shell injection to get wrong.
            timeout_s: Seconds before the process is killed.
            check: Raise :class:`CommandFailedError` on a non-zero exit.
            requires_admin: Verify elevation *before* launching. Produces a
                clear "run as Administrator" message instead of a confusing
                access-denied from inside the tool.
            operation: Plain-language description for the privilege error.
            remedy: Suggested fix attached to a ``check`` failure.
            encoding: Override the output codepage for this call. Legacy
                console tools emit the OEM codepage (the default), but a
                program told to emit UTF-8 must be decoded as UTF-8 —
                decoding one as the other produces silent mojibake.

        Raises:
            PrivilegeError: ``requires_admin`` and the process is not elevated.
            CommandNotFoundError: The executable does not exist.
            CommandTimeoutError: The process exceeded ``timeout_s``.
            CommandFailedError: ``check`` is set and the exit code is non-zero.
        """
        if not args:
            raise ValueError("args must not be empty")

        argv = [str(a) for a in args]
        timeout = self.default_timeout_s if timeout_s is None else timeout_s

        if requires_admin:
            require_admin(operation or f"run {argv[0]}")

        resolved = resolve_executable(argv[0])
        if resolved is None:
            raise CommandNotFoundError(argv[0], context={"argv": argv})
        argv[0] = resolved

        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, shell=False
                argv,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                shell=False,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            self._audit(argv, exit_code=None, duration_s=duration, timed_out=True)
            _log.warning("command timed out after %.1fs: %s", duration, " ".join(argv))
            raise CommandTimeoutError(
                " ".join(argv), timeout, context={"argv": argv}
            ) from exc
        except FileNotFoundError as exc:
            raise CommandNotFoundError(argv[0], context={"argv": argv}) from exc
        except OSError as exc:
            duration = time.monotonic() - started
            raise CommandFailedError(
                " ".join(argv),
                exit_code=-1,
                stderr=str(exc),
                remedy=remedy,
                context={"argv": argv, "duration_s": duration},
            ) from exc

        duration = time.monotonic() - started
        result = CommandResult(
            command=tuple(argv),
            exit_code=completed.returncode,
            stdout=self._decode(completed.stdout, encoding),
            stderr=self._decode(completed.stderr, encoding),
            duration_s=duration,
        )

        self._audit(
            argv, exit_code=result.exit_code, duration_s=duration, timed_out=False
        )
        _log.debug(
            "command exit=%s in %.2fs: %s",
            result.exit_code,
            duration,
            result.display_command,
        )

        if check and not result.ok:
            raise CommandFailedError(
                result.display_command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                remedy=remedy,
                context={"argv": argv, "stdout": result.stdout[:2000]},
            )
        return result

    def spawn(self, args: Sequence[str], *, cwd: str | None = None) -> SpawnedProcess:
        """Start a program and return without waiting for it.

        The same resolution and no-shell rules as :meth:`run`. The caller
        owns the process and must :meth:`SpawnedProcess.stop` it.

        Raises:
            CommandNotFoundError: The executable does not exist.
            CommandFailedError: The OS refused to start it.
        """
        if not args:
            raise ValueError("args must not be empty")
        argv = [str(a) for a in args]
        resolved = resolve_executable(argv[0])
        if resolved is None:
            raise CommandNotFoundError(argv[0], context={"argv": argv})
        argv[0] = resolved
        try:
            popen = subprocess.Popen(  # noqa: S603 - argv list, shell=False
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise CommandFailedError(
                " ".join(argv), exit_code=-1, stderr=str(exc),
                context={"argv": argv},
            ) from exc
        _log.debug("spawned pid %s: %s", popen.pid, " ".join(argv))
        return SpawnedProcess(popen)

    def try_run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        encoding: str | None = None,
    ) -> CommandResult | None:
        """Run a command, returning ``None`` instead of raising.

        For optional probes during a scan: a missing ``nvidia-smi`` on an AMD
        machine is an expected absence, not a failure worth interrupting a
        scan over.
        """
        try:
            return self.run(args, timeout_s=timeout_s, encoding=encoding)
        except (CommandNotFoundError, CommandTimeoutError, CommandFailedError) as exc:
            _log.debug("optional command unavailable: %s", exc.what)
            return None

    def _decode(self, raw: bytes | None, encoding: str | None = None) -> str:
        """Decode process output, never raising on malformed bytes.

        ``errors='replace'`` is deliberate: a mangled character in a log line
        must not abort a scan.
        """
        if not raw:
            return ""
        codec = encoding or self.encoding
        try:
            return raw.decode(codec, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def _audit(
        self,
        argv: Sequence[str],
        *,
        exit_code: int | None,
        duration_s: float,
        timed_out: bool,
    ) -> None:
        if self.audit is None:
            return
        try:
            self.audit(
                {
                    "module": "command_runner",
                    "operation": "run",
                    "target": argv[0],
                    "argv": list(argv),
                    "exit_code": exit_code,
                    "duration_s": round(duration_s, 3),
                    "timed_out": timed_out,
                }
            )
        except Exception:  # noqa: BLE001 - auditing must never break the operation
            _log.exception("audit sink raised; continuing")
