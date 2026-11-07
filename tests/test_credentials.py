"""Tests for existing credentials using synthetic commands only."""

import base64
from dataclasses import fields

import pytest

from custom_components.tuya_ble_lock.credentials import UnlockCredential, decode_unlock_command


def encoded_command(member=258, code=b"00001234", *, marker=b"\x01", trailer=b"\x00\x00"):
    """Encode a synthetic command without using any cloud or hardware."""
    return base64.b64encode(marker + member.to_bytes(2, "big") + code + trailer).decode()


@pytest.mark.parametrize("member", [1, 258, 65534])
@pytest.mark.parametrize("trailer", [b"\x00\x00", b"\x00\x01"])
def test_decode_preserves_member_and_leading_zeroes_without_retaining_payload(member, trailer):
    """Keep only the two fields needed for a subsequent local command."""
    value = encoded_command(member=member, trailer=trailer)
    result = decode_unlock_command(value)
    assert result.member == member
    assert result.code == "00001234"
    assert [field.name for field in fields(result)] == ["member", "code"]
    assert value not in repr(result)
    assert result.code not in repr(result)
    assert result.code not in str(result)
    assert result == UnlockCredential(member=member, code="00001234")


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        b"AQECAAAAMDAwMDAwMDA=",
        "",
        "[masked]",
        "*" * 20,
        "é" * 20,
        encoded_command() + "\n",
        " " + encoded_command(),
        encoded_command()[:-1],
        encoded_command()[:-2] + "A=",
        encoded_command()[:-3] + "B==",
        encoded_command(member=0),
        encoded_command(member=65535),
        encoded_command(marker=b"\x00"),
        encoded_command(trailer=b"\x00\x02"),
        encoded_command(trailer=b"\x01\x00"),
        encoded_command(trailer=b"\x00"),
        encoded_command(trailer=b"\x00\x00\x00"),
        encoded_command(code=b"1234567"),
        encoded_command(code=b"123456789"),
        encoded_command(code=b"1234567a"),
        encoded_command(code=b"1234567\xff"),
        encoded_command(code=b"1234567\x00"),
        encoded_command(code="１２３４".encode()),
    ],
)
def test_decode_rejects_invalid_commands_with_a_fixed_safe_error(value):
    """Reject noncanonical encodings, masked values, and unsupported layouts."""
    with pytest.raises(ValueError) as error:
        decode_unlock_command(value)
    assert str(error.value) == "Invalid unlock command"
    assert "00001234" not in repr(error.value)


def test_acknowledgement_does_not_contain_a_credential():
    """A DP61 acknowledgement cannot be imported as an unlock command."""
    with pytest.raises(ValueError, match="Invalid unlock command"):
        decode_unlock_command(base64.b64encode(b"\x00\x01\x02").decode())
