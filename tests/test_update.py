"""Tests for the in-app update.

The network is faked, the "program" is a file in a temporary folder, and the
signatures are made with a throwaway key, so nothing here contacts GitHub,
replaces the real executable or starts a process. What they pin down is what
a bad update can do: nothing. A wrong signature, a renamed old release, a
foreign address or a busy PC must leave the installed program untouched.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import threading

import pytest
from Cryptodome.PublicKey import ECC
from Cryptodome.Signature import eddsa

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer

from pudge_gaming_manager.core.update import updater as updater_module
from pudge_gaming_manager.core.update.signature import (
    UpdateSignatureError,
    signed_message,
    verify_update,
)
from pudge_gaming_manager.core.update.updater import (
    RELEASES_PAGE,
    Release,
    Updater,
    parse_version,
    remove_leftovers,
)
from pudge_gaming_manager.utilities import fetch
from pudge_gaming_manager.utilities.exceptions import PgmError

BASE = f"https://github.com/{updater_module.REPO}/releases/download"
NEW_BYTES = b"MZ the new program"
OLD_BYTES = b"MZ the old program"

_KEY = ECC.generate(curve="Ed25519")
PUBLIC = _KEY.public_key().export_key(format="raw").hex()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(version: str, data: bytes, key=_KEY) -> str:
    return eddsa.new(key, "rfc8032").sign(signed_message(version, _sha(data))).hex()


# -- versions ----------------------------------------------------------------------


def test_versions_order_prereleases_before_their_release() -> None:
    order = ["1.0.0-dev.4", "1.0.0-dev.5", "1.0.0-dev.10", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
    keys = [parse_version(v) for v in order]

    assert keys == sorted(keys)
    assert parse_version("v1.0.0-dev.5") == parse_version("1.0.0-dev.5")


@pytest.mark.parametrize("bad", ["", "1.0", "latest", "1.0.0-dev", "1.0.0\n-x", "x1.0.0"])
def test_a_malformed_version_is_not_a_version(bad: str) -> None:
    assert parse_version(bad) is None


# -- choosing a release -----------------------------------------------------------------


def _release(version: str, *, signed: bool = True, draft: bool = False, name: str | None = None) -> dict:
    asset = name or f"PudgeCleaner-{version}.exe"
    assets = [{"name": asset, "browser_download_url": f"{BASE}/v{version}/{asset}", "size": 1234}]
    if signed:
        assets.append({"name": asset + ".sig", "browser_download_url": f"{BASE}/v{version}/{asset}.sig"})
    return {"draft": draft, "body": f"notes {version}", "assets": assets, "html_url": f"{RELEASES_PAGE}/tag/v{version}"}


def _updater(releases, *, current="1.0.0-dev.5", exe=None, key=PUBLIC, **kw) -> Updater:
    def get_json(_url):
        if isinstance(releases, Exception):
            raise releases
        return releases

    return Updater(current=current, public_key=key, exe=exe, get_json=get_json, **kw)


def test_the_newest_signed_release_wins_whatever_the_list_order() -> None:
    info = _updater([_release("1.0.0-dev.6"), _release("1.0.0-dev.8"), _release("1.0.0-dev.7")]).check()

    assert info.available and info.latest.version == "1.0.0-dev.8"
    assert info.latest.notes == "notes 1.0.0-dev.8"


def test_an_unsigned_or_draft_release_is_not_offered() -> None:
    info = _updater([_release("1.0.0-dev.9", signed=False), _release("1.0.0-dev.8", draft=True)]).check()

    assert not info.available and info.latest is None


def test_an_unrelated_asset_name_is_ignored() -> None:
    info = _updater([_release("1.0.0-dev.9", name="Other-1.0.0-dev.9.exe")]).check()

    assert info.latest is None


def test_the_same_or_an_older_release_is_not_an_update() -> None:
    assert not _updater([_release("1.0.0-dev.5")]).check().available
    assert not _updater([_release("1.0.0-dev.4")]).check().available


def test_a_release_after_a_prerelease_is_an_update() -> None:
    assert _updater([_release("1.0.0")]).check().available


def test_being_offline_is_no_news_not_an_error() -> None:
    info = _updater(fetch.FetchError("нет сети", "timeout")).check()

    assert not info.available and not info.checked


def test_garbage_from_the_server_is_no_news() -> None:
    assert not _updater({"message": "rate limited"}).check().available


def test_a_source_checkout_reports_updates_but_cannot_install(tmp_path: pathlib.Path) -> None:
    info = _updater([_release("1.0.1")], exe=None).check()

    assert info.available and not info.can_install and info.page


def test_a_build_without_a_key_cannot_install(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "PudgeCleaner.exe"

    assert not _updater([_release("1.0.1")], exe=exe, key="").check().can_install
    assert _updater([_release("1.0.1")], exe=exe).check().can_install


# -- the signature -----------------------------------------------------------------------


def test_a_genuine_signature_verifies() -> None:
    verify_update(PUBLIC, "1.0.1", _sha(NEW_BYTES), _sign("1.0.1", NEW_BYTES) + "\n")


def test_an_old_release_cannot_pass_as_a_newer_version() -> None:
    """The downgrade attack: a validly signed 1.0.0, renamed 9.9.9."""
    signature = _sign("1.0.0", NEW_BYTES)

    with pytest.raises(UpdateSignatureError):
        verify_update(PUBLIC, "9.9.9", _sha(NEW_BYTES), signature)


def test_a_different_file_does_not_verify() -> None:
    with pytest.raises(UpdateSignatureError):
        verify_update(PUBLIC, "1.0.1", _sha(b"something else"), _sign("1.0.1", NEW_BYTES))


def test_another_key_does_not_verify() -> None:
    stranger = ECC.generate(curve="Ed25519")

    with pytest.raises(UpdateSignatureError):
        verify_update(PUBLIC, "1.0.1", _sha(NEW_BYTES), _sign("1.0.1", NEW_BYTES, stranger))


@pytest.mark.parametrize("signature", ["", "zz" * 64, "ab" * 10])
def test_an_unreadable_signature_is_refused(signature: str) -> None:
    with pytest.raises(UpdateSignatureError):
        verify_update(PUBLIC, "1.0.1", _sha(NEW_BYTES), signature)


def test_no_key_means_nothing_verifies() -> None:
    with pytest.raises(UpdateSignatureError):
        verify_update("", "1.0.1", _sha(NEW_BYTES), _sign("1.0.1", NEW_BYTES))


# -- installing ----------------------------------------------------------------------------


class Rig:
    def __init__(self, tmp_path: pathlib.Path, *, version="1.0.1", payload=NEW_BYTES, signature=None,
                 url=None, key=PUBLIC) -> None:
        self.exe = tmp_path / "PudgeCleaner.exe"
        self.exe.write_bytes(OLD_BYTES)
        self.spawned: list[list[str]] = []
        self.progress: list = []
        self.release = Release(
            version, "notes", url or f"{BASE}/v{version}/PudgeCleaner-{version}.exe",
            f"{BASE}/v{version}/PudgeCleaner-{version}.exe.sig", len(payload), RELEASES_PAGE,
        )
        signature = _sign(version, NEW_BYTES) if signature is None else signature
        self.downloads: list[str] = []

        def download(url, destination, report, limit):
            self.downloads.append(url)
            destination.write_bytes(payload)
            report(0.5)
            report(1.0)

        self.updater = Updater(
            current="1.0.0-dev.5", public_key=key, exe=self.exe,
            read_text=lambda _url: signature, download=download,
            spawn=lambda argv: self.spawned.append(argv),
        )

    def install(self, blocker=lambda: None) -> None:
        self.updater.install(self.release, self.progress.append, blocker)

    def beside(self) -> set[str]:
        return {p.name for p in self.exe.parent.iterdir()}


def test_a_signed_update_replaces_the_program_and_starts_it(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path)

    rig.install()

    assert rig.exe.read_bytes() == NEW_BYTES
    assert (tmp_path / "PudgeCleaner.exe.old").read_bytes() == OLD_BYTES
    assert rig.spawned == [[str(rig.exe)]]
    assert "PudgeCleaner.exe.new" not in rig.beside()


def test_progress_reports_the_download_then_the_swap(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path)

    rig.install()

    texts = [p.text for p in rig.progress]
    assert any("Скачиваю" in t for t in texts) and texts[-1].startswith("Заменяю")
    assert [p.fraction for p in rig.progress[:2]] == [0.5, 1.0]


def test_a_bad_signature_changes_nothing(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path, payload=b"MZ tampered")

    with pytest.raises(PgmError, match="отклонено"):
        rig.install()

    assert rig.exe.read_bytes() == OLD_BYTES and rig.spawned == []
    assert rig.beside() == {"PudgeCleaner.exe"}


def test_a_renamed_old_release_is_refused(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path, version="9.9.9", signature=_sign("1.0.0", NEW_BYTES))

    with pytest.raises(PgmError):
        rig.install()

    assert rig.exe.read_bytes() == OLD_BYTES


@pytest.mark.parametrize(
    "url",
    ["https://evil.example/PudgeCleaner-1.0.1.exe",
     "https://github.com/someone-else/Pudge-Cleaner/releases/download/v1/PudgeCleaner-1.0.1.exe",
     "http://github.com/x"],
)
def test_a_foreign_address_is_never_downloaded(tmp_path: pathlib.Path, url: str) -> None:
    rig = Rig(tmp_path, url=url)

    with pytest.raises(PgmError):
        rig.install()

    assert rig.downloads == [] and rig.exe.read_bytes() == OLD_BYTES


def test_a_busy_pc_postpones_the_update_after_the_download(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path)

    with pytest.raises(PgmError, match="отложено"):
        rig.install(blocker=lambda: "Идёт очистка диска.")

    assert rig.exe.read_bytes() == OLD_BYTES and rig.spawned == []
    assert rig.beside() == {"PudgeCleaner.exe"}


def test_without_a_key_nothing_is_downloaded(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path, key="")

    with pytest.raises(PgmError, match="ключ"):
        rig.install()

    assert rig.downloads == []


def test_from_source_there_is_nothing_to_replace(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path)
    rig.updater._exe = None

    with pytest.raises(PgmError, match="исходников"):
        rig.install()


def test_if_the_new_file_cannot_take_the_name_the_old_one_comes_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    real_replace = os.replace
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # staged -> exe
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(updater_module.os, "replace", replace)

    with pytest.raises(PgmError):
        rig.install()

    assert rig.exe.read_bytes() == OLD_BYTES and rig.spawned == []
    assert rig.beside() == {"PudgeCleaner.exe"}


def test_a_failed_start_says_the_update_is_in_place(tmp_path: pathlib.Path) -> None:
    rig = Rig(tmp_path)

    def spawn(_argv):
        raise PgmError("не запустилось", "отказано")

    rig.updater._spawn = spawn

    with pytest.raises(PgmError, match="обновлена"):
        rig.install()

    assert rig.exe.read_bytes() == NEW_BYTES


def test_leftovers_from_an_update_are_removed(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "PudgeCleaner.exe"
    exe.write_bytes(b"x")
    for suffix in (".old", ".new"):
        (tmp_path / f"PudgeCleaner.exe{suffix}").write_bytes(b"y")

    remove_leftovers(exe)

    assert {p.name for p in tmp_path.iterdir()} == {"PudgeCleaner.exe"}


def test_a_source_checkout_removes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    remove_leftovers()  # must simply return


# -- the network helper -----------------------------------------------------------------------


def test_only_https_is_fetched() -> None:
    with pytest.raises(fetch.FetchError):
        fetch.read_text("http://example.com/x")


class _Response:
    def __init__(self, body: bytes, length: int | None = None) -> None:
        self._body, self._pos = body, 0
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def read(self, n=-1):
        chunk = self._body[self._pos:] if n < 0 else self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_a_download_past_its_limit_is_refused(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_open", lambda _url: _Response(b"x" * 5000))

    with pytest.raises(fetch.FetchError, match="большой"):
        fetch.download("https://x", tmp_path / "f", lambda _f: None, limit=1000)


def test_a_download_reports_its_fraction(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_open", lambda _url: _Response(b"x" * 100, length=100))
    seen: list = []

    fetch.download("https://x", tmp_path / "f", seen.append, limit=1000)

    assert seen[-1] == 1.0 and (tmp_path / "f").read_bytes() == b"x" * 100


# -- the signing script ----------------------------------------------------------------------------


def test_the_signing_script_signs_what_the_program_verifies(tmp_path: pathlib.Path) -> None:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    import update_signing as script

    public_file = tmp_path / "key.py"
    public_file.write_text('UPDATE_PUBLIC_KEY = ""\n', encoding="utf-8")
    exe = tmp_path / "PudgeCleaner-1.0.1.exe"
    exe.write_bytes(NEW_BYTES)
    key_path = tmp_path / "keys" / "k.pem"

    public = script.keygen(key_path, "correct horse battery", public_file)
    script.sign_file(exe, "1.0.1", key_path, "correct horse battery", public_file)
    script.verify_file(exe, "1.0.1", public_file)

    assert script.read_public_key(public_file) == public
    with pytest.raises(script.SigningError):
        script.verify_file(exe, "1.0.2", public_file)
    with pytest.raises(script.SigningError):
        script.sign_file(exe, "1.0.1", key_path, "wrong passphrase!!", public_file)
    with pytest.raises(script.SigningError, match="already exists"):
        script.keygen(key_path, "correct horse battery", public_file)


# -- the controller -----------------------------------------------------------------------------------


def _wait(until, timeout_ms: int = 5000) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: until() and loop.quit())
    timer.start(10)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()


class _FakeUpdater:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.fail = False
        self.blockers: list = []

    def check(self):
        return "info"

    def install(self, release, progress, blocker):
        self.blockers.append(blocker())
        self.release.wait(5)
        if self.fail:
            raise PgmError("не вышло", "нет места")


def test_the_controller_reports_a_check(app) -> None:
    from pudge_gaming_manager.app.controllers.update_controller import UpdateController

    controller = UpdateController(updater=_FakeUpdater())
    got: list = []
    controller.checked.connect(got.append)

    controller.check()
    _wait(lambda: got)

    assert got == ["info"]


def test_installing_blocks_closing_until_it_ends_then_asks_to_close(app) -> None:
    from pudge_gaming_manager.app.controllers.update_controller import UpdateController

    fake = _FakeUpdater()
    controller = UpdateController(updater=fake)
    done: list = []
    controller.installed.connect(lambda: done.append(1))

    controller.install("release", lambda: None)
    _wait(lambda: fake.blockers)

    assert controller.installing
    fake.release.set()
    _wait(lambda: done)

    assert done and not controller.installing


def test_a_failed_install_is_reported_and_stops_blocking(app) -> None:
    from pudge_gaming_manager.app.controllers.update_controller import UpdateController

    fake = _FakeUpdater()
    fake.fail = True
    fake.release.set()
    controller = UpdateController(updater=fake)
    errors: list = []
    controller.failed.connect(errors.append)

    controller.install("release", lambda: None)
    _wait(lambda: errors)

    assert errors and "нет места" in errors[0] and not controller.installing
