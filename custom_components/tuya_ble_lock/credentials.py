"""Decode an existing supported command without creating lock credentials."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, repr=False)
class UnlockCredential:
    """The minimal secret needed for local unlocking, excluded from repr."""

    member: int
    code: str


def decode_unlock_command(value: str) -> UnlockCredential:
    """Parse one canonical Base64 DP61 command without exposing its contents."""
    if not isinstance(value, str) or len(value) != 20 or not value.isascii():
        raise ValueError("Invalid unlock command")
    try:
        raw = base64.b64decode(value, validate=True)
    except binascii.Error, ValueError:
        raise ValueError("Invalid unlock command") from None
    # The app uses source bytes 00 01; local commands use 00 00.
    if (
        len(raw) != 13
        or raw[0] != 1
        or raw[-2:] not in (b"\x00\x00", b"\x00\x01")
        or base64.b64encode(raw).decode("ascii") != value
        or any(not 48 <= character <= 57 for character in raw[3:11])
    ):
        raise ValueError("Invalid unlock command")
    member = int.from_bytes(raw[1:3], "big")
    if not 1 <= member <= 65534:
        raise ValueError("Invalid unlock command")
    return UnlockCredential(member=member, code=raw[3:11].decode("ascii"))
