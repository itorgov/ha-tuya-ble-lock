"""Exercise actual encrypted TinyTuya frames without opening network sockets."""

import base64
import hmac
import json
import logging
import time
from hashlib import sha256
from unittest.mock import Mock

import pytest
import tinytuya
from tinytuya.core import header
from tinytuya.core.crypto_helper import AESCipher
from tinytuya.core.exceptions import DecodeError
from tinytuya.core.message_helper import MessagePayload, TuyaMessage, pack_message, unpack_message

from custom_components.tuya_ble_lock import local

KEY = b"0123456789abcdef"


class BufferSocket:
    """A byte stream containing only synthetic, in-memory server frames."""

    def __init__(self, data=b""):
        self.data = data
        self.sent = []

    def recv(self, length):
        if not self.data:
            raise TimeoutError("No synthetic frame available.")
        result, self.data = self.data[:length], self.data[length:]
        return result

    def settimeout(self, timeout):
        pass

    def close(self):
        pass


def gateway_for(version):
    return local._GatewayDevice(
        "gateway_example", "192.0.2.1", KEY.decode(), version=version, persist=True
    )


def server_frame(gateway, command, payload, *, corrupt=False):
    """Frame a server response, optionally corrupting its final integrity byte."""
    if command not in header.NO_PROTOCOL_HEADER_CMDS:
        payload = gateway.version_header + payload
    if gateway.version >= 3.5:
        message = TuyaMessage(
            7, command, 0, payload, 0, True, header.PREFIX_6699_VALUE, b"server_nonce"
        )
        frame = pack_message(message, hmac_key=gateway.local_key)
    else:
        if gateway.version == 3.3 and command not in header.NO_PROTOCOL_HEADER_CMDS:
            encrypted = gateway.version_header + AESCipher(gateway.local_key).encrypt(
                payload[len(gateway.version_header) :], False
            )
        else:
            encrypted = AESCipher(gateway.local_key).encrypt(payload, False)
        message = TuyaMessage(
            7, command, 0, b"\0" * 4 + encrypted, 0, True, header.PREFIX_55AA_VALUE, None
        )
        frame = pack_message(
            message, hmac_key=gateway.local_key if gateway.version >= 3.4 else None
        )
    if corrupt:
        frame = frame[:-5] + bytes([frame[-5] ^ 1]) + frame[-4:]
    return frame


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_gcm_frames_use_fresh_random_ivs_and_preserve_sequence_and_payload(caplog, level):
    caplog.set_level(level, logger="tinytuya.core.crypto_helper")
    gateway = gateway_for(3.5)
    child = tinytuya.Device("lock_example", cid="node_example", parent=gateway)
    original_sequence = gateway.seqno
    commands = [
        MessagePayload(tinytuya.SESS_KEY_NEG_START, b"client_nonce_123"),
        MessagePayload(tinytuya.SESS_KEY_NEG_FINISH, b"synthetic_challenge_response"),
        gateway.generate_payload(tinytuya.HEART_BEAT),
        child.generate_payload(
            tinytuya.CONTROL,
            {"61": base64.b64encode(local.build_unlock(1, "12345678")).decode()},
        ),
    ]
    decoded = [
        unpack_message(child._encode_message(command), hmac_key=KEY, no_retcode=True)
        for command in commands
    ]
    assert len({message.iv for message in decoded}) == len(commands)
    assert all(len(message.iv) == 12 and message.iv != b"0123456789ab" for message in decoded)
    assert [message.seqno for message in decoded] == list(
        range(original_sequence, original_sequence + len(commands))
    )
    assert gateway.seqno == original_sequence + len(commands)
    for command, message in zip(commands, decoded, strict=True):
        expected = command.payload
        if command.cmd not in header.NO_PROTOCOL_HEADER_CMDS:
            expected = gateway.version_header + expected
        assert message.crc_good
        assert message.cmd == command.cmd
        assert message.payload == expected
    data = json.loads(decoded[-1].payload[len(gateway.version_header) :])["data"]
    assert data["cid"] == "node_example"
    assert data["dps"] == {"61": "AQABMTIzNDU2NzgAAA=="}
    gateway.close()


@pytest.mark.parametrize("version", [3.3, 3.4])
def test_older_protocol_encoding_delegates_to_vendor_unchanged(version):
    gateway = gateway_for(version)
    vendor = tinytuya.Device("gateway_example", "192.0.2.1", KEY.decode(), version=version)
    payload = MessagePayload(tinytuya.CONTROL, b'{"dps":{"61":"synthetic"}}')
    assert gateway._encode_message(payload) == vendor._encode_message(payload)
    assert gateway.seqno == vendor.seqno
    gateway.close()
    vendor.close()


@pytest.mark.parametrize("version", [3.3, 3.4, 3.5])
def test_valid_real_server_frames_decode(version):
    gateway = gateway_for(version)
    gateway.socket = BufferSocket(
        server_frame(gateway, tinytuya.STATUS, b'{"cid":"node_example","dps":{"47":false}}')
    )
    message = gateway._receive()
    assert message.crc_good
    assert gateway._decode_payload(message.payload) == {"cid": "node_example", "dps": {"47": False}}
    gateway.close()


@pytest.mark.parametrize("version", [3.3, 3.4, 3.5])
def test_bad_frame_integrity_is_rejected_before_decoding(version, monkeypatch):
    gateway = gateway_for(version)
    gateway.socket = BufferSocket(
        server_frame(
            gateway,
            tinytuya.STATUS,
            b'{"cid":"node_example","dps":{"47":false}}',
            corrupt=True,
        )
    )
    gateway._decode_payload = Mock(side_effect=AssertionError("Must not decode an invalid frame"))
    monkeypatch.setattr(local.select, "select", lambda *args: ([gateway.socket], [], []))
    with pytest.raises(local.CannotConnect, match="response was interrupted"):
        list(local.GatewayClient._read_decoded(gateway, time.monotonic() + 1))
    gateway._decode_payload.assert_not_called()
    gateway.close()


@pytest.mark.parametrize("version", [3.4, 3.5])
@pytest.mark.parametrize("corrupt", [False, True])
def test_real_handshake_authenticates_before_accepting_remote_nonce(caplog, version, corrupt):
    caplog.set_level(logging.DEBUG, logger="tinytuya.core.crypto_helper")
    gateway = gateway_for(version)
    remote_nonce = b"fedcba9876543210"
    stream = BufferSocket()
    gateway.socket = stream

    def sendall(frame):
        message = unpack_message(frame, hmac_key=KEY, no_retcode=True)
        assert message.crc_good
        stream.sent.append(message)
        payload = message.payload
        if version == 3.4:
            payload = AESCipher(KEY).decrypt(payload, False, decode_text=False)
        if message.cmd == tinytuya.SESS_KEY_NEG_START:
            assert len(payload) == 16
            # The internal challenge HMAC is valid; only the outer frame is
            # corrupted. The remote nonce must not be accepted on that basis.
            response = remote_nonce + hmac.new(KEY, payload, sha256).digest()
            stream.data += server_frame(
                gateway, tinytuya.SESS_KEY_NEG_RESP, response, corrupt=corrupt
            )
        else:
            assert message.cmd == tinytuya.SESS_KEY_NEG_FINISH
            assert payload == hmac.new(KEY, remote_nonce, sha256).digest()

    stream.sendall = sendall
    assert gateway._negotiate_session_key() is not corrupt
    if corrupt:
        assert gateway.local_key == KEY
        assert gateway.remote_nonce == b""
        assert [message.cmd for message in stream.sent] == [tinytuya.SESS_KEY_NEG_START]
    else:
        assert gateway.local_key != KEY
        assert gateway.remote_nonce == remote_nonce
        assert [message.cmd for message in stream.sent] == [
            tinytuya.SESS_KEY_NEG_START,
            tinytuya.SESS_KEY_NEG_FINISH,
        ]
        assert [message.seqno for message in stream.sent] == [1, 2]
        if version == 3.5:
            assert stream.sent[0].iv != stream.sent[1].iv
    gateway.close()


def test_bad_hmac_rejected_at_gateway_boundary_before_handshake_processing():
    gateway = gateway_for(3.4)
    gateway._negotiate_session_key_generate_step_1()
    payload = b"fedcba9876543210" + hmac.new(KEY, gateway.local_nonce, sha256).digest()
    gateway.socket = BufferSocket(
        server_frame(gateway, tinytuya.SESS_KEY_NEG_RESP, payload, corrupt=True)
    )
    with pytest.raises(DecodeError, match="integrity verification failed"):
        gateway._receive()
    gateway.close()
