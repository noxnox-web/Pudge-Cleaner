"""Checks that an update was signed by the program's publisher.

An update is an executable the program downloads and then *becomes*, running
as Administrator. A checksum published next to it proves the download arrived
intact, not who made it: anyone who can write to the releases page -- a
stolen GitHub token is enough -- can upload a different file with a matching
checksum. The signature is made with a key that never goes near GitHub, and
the program only installs what that key signed.

What is signed is not the file but a short statement about it -- the version
and the file's SHA-256 -- so an old release, which the key really did sign,
cannot be passed off as a newer version by renaming it (a downgrade).
"""

from __future__ import annotations

import re

from Cryptodome.Signature import eddsa

#: ``1.2.3`` or ``1.0.0-dev.5``.
VERSION = r"\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?"

# fullmatch, not ``^...$``: ``$`` also matches before a trailing newline,
# which would let "1.2.3\n" into the signed message.
_VERSION = re.compile(VERSION)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_SIGNATURE_HEX = re.compile(r"[0-9a-f]{128}")
_PUBLIC_KEY_HEX = re.compile(r"[0-9a-f]{64}")


class UpdateSignatureError(ValueError):
    pass


def signed_message(version: str, sha256_hex: str) -> bytes:
    """The exact bytes the publisher signs for one update, and the program checks."""
    if not _VERSION.fullmatch(version):
        raise UpdateSignatureError(f"{version!r} is not a version number.")
    if not _SHA256_HEX.fullmatch(sha256_hex):
        raise UpdateSignatureError("The file's SHA-256 is not 64 lowercase hex digits.")
    return f"pudge-cleaner-update/1\n{version}\n{sha256_hex}\n".encode("ascii")


def verify_update(public_key_hex: str, version: str, sha256_hex: str, signature_text: str) -> None:
    """Returns when ``signature_text`` is the publisher's signature of this
    version and file; raises :class:`UpdateSignatureError` otherwise."""
    key_hex = public_key_hex.strip().lower()
    if not _PUBLIC_KEY_HEX.fullmatch(key_hex):
        raise UpdateSignatureError("This build has no valid key to check updates against.")
    signature_hex = signature_text.strip().lower()
    if not _SIGNATURE_HEX.fullmatch(signature_hex):
        raise UpdateSignatureError("The update's signature file is unreadable.")
    message = signed_message(version, sha256_hex)
    try:
        key = eddsa.import_public_key(bytes.fromhex(key_hex))
        eddsa.new(key, "rfc8032").verify(message, bytes.fromhex(signature_hex))
    except ValueError as exc:
        raise UpdateSignatureError("The update's signature does not match.") from exc


__all__ = ["VERSION", "UpdateSignatureError", "signed_message", "verify_update"]
