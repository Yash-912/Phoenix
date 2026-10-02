"""clear_approved_cache: refusals, and that the Redis reply is actually read.

The bug these tests pin: the function used to return {"status": "ok"}
unconditionally once sendall() succeeded, discarding whatever Redis actually
replied with -- so a rejected DEL still reported success. These tests fail
against that version and pass against the fix.
"""

from __future__ import annotations

import socket

import pytest

from phoenix.tools import remediation_tool


class _FakeSocket:
    """Stands in for socket.create_connection. Records what was sent, answers
    with whatever reply this test configured."""

    def __init__(self, reply: bytes = b":1\r\n", raise_on_send: Exception | None = None):
        self.reply = reply
        self.raise_on_send = raise_on_send
        self.sent: bytes = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent = data

    def recv(self, bufsize: int) -> bytes:
        return self.reply

    def close(self) -> None:
        self.closed = True


def _patch_connection(monkeypatch, fake: _FakeSocket) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)


# --- refusals, before any socket is touched ---------------------------------


def test_an_empty_key_is_refused_without_touching_the_socket(monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection",
        lambda *a, **k: pytest.fail("must not connect for an invalid key"),
    )

    result = remediation_tool.clear_approved_cache("")

    assert result["status"] == "error"
    assert "non-empty string" in result["error"]


@pytest.mark.parametrize("key", ["*", "flushall", "FLUSHALL", "FLUSHDB", "cache:*"])
def test_a_wildcard_or_flush_key_is_refused_without_touching_the_socket(key, monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection",
        lambda *a, **k: pytest.fail(f"must not connect for {key!r}"),
    )

    result = remediation_tool.clear_approved_cache(key)

    assert result["status"] == "error"
    assert "wildcard/flush not allowed" in result["error"]


# --- the Redis reply is read, not assumed ------------------------------------


def test_an_integer_reply_of_one_is_a_successful_delete(monkeypatch):
    fake = _FakeSocket(reply=b":1\r\n")
    _patch_connection(monkeypatch, fake)

    result = remediation_tool.clear_approved_cache("session:42")

    assert result["status"] == "ok"
    assert result["deleted"] is True


def test_an_integer_reply_of_zero_is_still_ok_the_key_was_already_gone(monkeypatch):
    fake = _FakeSocket(reply=b":0\r\n")
    _patch_connection(monkeypatch, fake)

    result = remediation_tool.clear_approved_cache("session:already-gone")

    assert result["status"] == "ok"
    assert result["deleted"] is False


def test_an_error_reply_is_reported_as_an_error_not_as_ok(monkeypatch):
    """The bug: the old code returned ok here because it never read this reply."""
    fake = _FakeSocket(reply=b"-ERR wrong number of arguments for 'del' command\r\n")
    _patch_connection(monkeypatch, fake)

    result = remediation_tool.clear_approved_cache("session:42")

    assert result["status"] == "error"
    assert "wrong number of arguments" in result["error"]


def test_an_unrecognized_reply_is_an_error_not_a_silent_ok(monkeypatch):
    fake = _FakeSocket(reply=b"+PONG\r\n")
    _patch_connection(monkeypatch, fake)

    result = remediation_tool.clear_approved_cache("session:42")

    assert result["status"] == "error"
    assert "unexpected reply" in result["error"]


def test_a_connection_refused_is_reported_as_an_error(monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError("refused")),
    )

    result = remediation_tool.clear_approved_cache("session:42")

    assert result["status"] == "error"
    assert "refused" in result["error"]


def test_the_socket_is_closed_even_when_the_reply_is_an_error(monkeypatch):
    fake = _FakeSocket(reply=b"-ERR no such key\r\n")
    _patch_connection(monkeypatch, fake)

    remediation_tool.clear_approved_cache("session:42")

    assert fake.closed is True


# --- the RESP frame is built from bytes, not characters ----------------------


def test_the_length_prefix_is_the_byte_length_not_the_character_length(monkeypatch):
    """A non-ASCII key has more UTF-8 bytes than Python characters. A prefix
    built from len(str) would frame the command wrong and corrupt the
    protocol for every byte that follows."""
    fake = _FakeSocket(reply=b":1\r\n")
    _patch_connection(monkeypatch, fake)
    key = "café"  # 4 chars, 5 UTF-8 bytes

    remediation_tool.clear_approved_cache(key)

    key_bytes = key.encode("utf-8")
    assert len(key_bytes) == 5
    assert f"${len(key_bytes)}\r\n".encode("ascii") + key_bytes + b"\r\n" in fake.sent


def test_an_ascii_key_builds_the_expected_resp_frame(monkeypatch):
    fake = _FakeSocket(reply=b":1\r\n")
    _patch_connection(monkeypatch, fake)

    remediation_tool.clear_approved_cache("session:42")

    assert fake.sent == b"*2\r\n$3\r\nDEL\r\n$10\r\nsession:42\r\n"
