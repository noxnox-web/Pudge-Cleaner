"""Signs Pudge Cleaner's in-app updates (see core/update/signature.py for why).

    python scripts/update_signing.py keygen
    python scripts/update_signing.py sign   dist/PudgeCleaner.exe --version 1.0.0
    python scripts/update_signing.py verify dist/PudgeCleaner.exe --version 1.0.0

`keygen` makes the key pair once. The private half is written, encrypted with a passphrase,
outside this repository -- it is the one secret that lets someone ship code to every install,
so it is never committed, synced or pasted anywhere -- and the public half goes into
pudge_gaming_manager/core/update/key.py, which is committed and ships inside the program.
`sign` writes `<file>.sig`; upload it to the release next to the exe. The passphrase is asked
for at the terminal; PGM_SIGNING_PASSPHRASE answers it where there is no terminal.

The signature covers the version and the file's SHA-256, not the file name, so the exe is
uploaded as PudgeCleaner-<version>.exe and signed as that version.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Cryptodome.PublicKey import ECC  # noqa: E402
from Cryptodome.Signature import eddsa  # noqa: E402

from pudge_gaming_manager.core.update.signature import (  # noqa: E402
    UpdateSignatureError,
    signed_message,
    verify_update,
)

DEFAULT_KEY_PATH = Path.home() / ".pudge-cleaner" / "update-signing-key.pem"
DEFAULT_PUBLIC_KEY_FILE = PROJECT_ROOT / "pudge_gaming_manager" / "core" / "update" / "key.py"
PASSPHRASE_VARIABLE = "PGM_SIGNING_PASSPHRASE"
_MIN_PASSPHRASE = 12
# PKCS#8 with scrypt and AES-256, which OpenSSL reads too. scrypt rather than PBKDF2: the
# library's PBKDF2 is pure Python (12 s for 600 000 rounds), scrypt is native and memory-hard.
_PROTECTION = "scryptAndAES256-CBC"
_SCRYPT_N = 2**17
_KEY_LINE = re.compile(r'^UPDATE_PUBLIC_KEY\s*=\s*"([0-9a-f]*)"\s*$', re.MULTILINE)

_PUBLIC_KEY_FILE_TEXT = '''"""The public half of the key that signs Pudge Cleaner's updates.

Written by `python scripts/update_signing.py keygen`; the private half never enters this
repository. 32 bytes as 64 hex characters. Empty until that command has been run -- a build
with no key shows that an update exists but refuses to install one.
"""

UPDATE_PUBLIC_KEY = "{key}"
'''


class SigningError(RuntimeError):
    pass


def read_public_key(public_key_file: Path = DEFAULT_PUBLIC_KEY_FILE) -> str:
    match = _KEY_LINE.search(public_key_file.read_text(encoding="utf-8"))
    if not match:
        raise SigningError(f"{public_key_file} has no UPDATE_PUBLIC_KEY line.")
    return match.group(1)


def _passphrase(*, confirm: bool) -> str:
    supplied = os.environ.get(PASSPHRASE_VARIABLE)
    if supplied is None:
        if not sys.stdin.isatty():
            raise SigningError(f"There is no terminal to ask for the passphrase on; set {PASSPHRASE_VARIABLE}.")
        supplied = getpass.getpass("Passphrase for the update signing key: ")
        if confirm and getpass.getpass("Again: ") != supplied:
            raise SigningError("The two passphrases differ.")
    if confirm and len(supplied) < _MIN_PASSPHRASE:
        raise SigningError(f"Use a passphrase of at least {_MIN_PASSPHRASE} characters.")
    return supplied


def keygen(key_path: Path, passphrase: str, public_key_file: Path = DEFAULT_PUBLIC_KEY_FILE) -> str:
    """Makes the key pair; returns the public key as hex."""
    key_path = key_path.expanduser().resolve()
    if key_path.is_relative_to(PROJECT_ROOT):
        raise SigningError(f"{key_path} is inside the repository. Keep the private key somewhere that is never committed.")
    if key_path.exists():
        raise SigningError(f"{key_path} already exists. A second key would orphan every build that carries the first one.")
    key = ECC.generate(curve="Ed25519")
    encrypted = key.export_key(
        format="PEM", passphrase=passphrase, use_pkcs8=True, protection=_PROTECTION, prot_params={"iteration_count": _SCRYPT_N}
    )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(encrypted, encoding="ascii")
    public_hex = key.public_key().export_key(format="raw").hex()
    public_key_file.write_text(_PUBLIC_KEY_FILE_TEXT.format(key=public_hex), encoding="utf-8")
    return public_hex


def _load_key(key_path: Path, passphrase: str) -> ECC.EccKey:
    try:
        key = ECC.import_key(key_path.expanduser().read_bytes(), passphrase=passphrase)
    except FileNotFoundError as exc:
        raise SigningError(f"{key_path} does not exist. Make the key first: python scripts/update_signing.py keygen") from exc
    except ValueError as exc:
        raise SigningError("The key could not be read: wrong passphrase, or not an update signing key.") from exc
    if key.curve != "Ed25519" or not key.has_private():
        raise SigningError("That is not an Ed25519 private key.")
    return key


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def sign_file(
    file: Path, version: str, key_path: Path, passphrase: str, public_key_file: Path = DEFAULT_PUBLIC_KEY_FILE
) -> Path:
    """Writes `<file>.sig` and checks it against the key the program carries, so a signature
    the program would refuse -- made with another key -- is caught here, not by every user."""
    public_key = read_public_key(public_key_file)
    if not public_key:
        raise SigningError("core/update/key.py has no public key yet. Run: python scripts/update_signing.py keygen")
    digest = sha256_of(file)
    key = _load_key(key_path, passphrase)
    signature = eddsa.new(key, "rfc8032").sign(signed_message(version, digest)).hex()
    try:
        verify_update(public_key, version, digest, signature)
    except UpdateSignatureError as exc:
        raise SigningError(
            "That key is not the one the program carries (core/update/key.py), so no install would accept this signature."
        ) from exc
    target = file.with_name(file.name + ".sig")
    target.write_text(signature + "\n", encoding="ascii")
    return target


def verify_file(file: Path, version: str, public_key_file: Path = DEFAULT_PUBLIC_KEY_FILE) -> None:
    signature = file.with_name(file.name + ".sig")
    try:
        verify_update(read_public_key(public_key_file), version, sha256_of(file), signature.read_text(encoding="ascii"))
    except FileNotFoundError as exc:
        raise SigningError(f"{exc.filename} was not found.") from exc
    except UpdateSignatureError as exc:
        raise SigningError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("keygen", help="make the signing key pair, once")
    sign = commands.add_parser("sign", help="write <file>.sig for a release exe")
    verify = commands.add_parser("verify", help="check <file>.sig against the key in core/update/key.py")
    for command in (make, sign, verify):
        command.add_argument(
            "--public-key-file", type=Path, default=DEFAULT_PUBLIC_KEY_FILE,
            help="where the program's public key lives (default core/update/key.py)",
        )
    for command in (make, sign):
        command.add_argument("--key", type=Path, default=DEFAULT_KEY_PATH, help=f"the private key (default {DEFAULT_KEY_PATH})")
    for command in (sign, verify):
        command.add_argument("file", type=Path)
        command.add_argument("--version", required=True, help="the version the exe is released as, e.g. 1.0.0 or 1.0.0-dev.6")
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "keygen":
            public_hex = keygen(arguments.key, _passphrase(confirm=True), arguments.public_key_file)
            print(f"Public key {public_hex}\nwritten to {arguments.public_key_file} -- commit that file.")
            print(f"Private key {arguments.key.expanduser().resolve()} (encrypted).")
            print(
                "Back it up and the passphrase somewhere safe, and keep both off GitHub and out of cloud sync: "
                "whoever holds them can ship code to every install. Lose them and every installed copy must be "
                "reinstalled by hand, because it will only accept updates signed with this key."
            )
        elif arguments.command == "sign":
            target = sign_file(arguments.file, arguments.version, arguments.key, _passphrase(confirm=False), arguments.public_key_file)
            print(f"Signed {arguments.file.name} as {arguments.version}: {target.name}")
        else:
            verify_file(arguments.file, arguments.version, arguments.public_key_file)
            print(f"{arguments.file.name} is signed by this program's key as {arguments.version}.")
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
