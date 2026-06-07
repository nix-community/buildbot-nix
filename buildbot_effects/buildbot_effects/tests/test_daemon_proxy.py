from __future__ import annotations

import shutil
import socket
import struct
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from buildbot_effects.daemon_proxy import nix_daemon_proxy

WORKER_MAGIC_1 = 0x6E697863  # "nixc": client hello
WORKER_MAGIC_2 = 0x6478696F  # "dxio": daemon reply


def test_proxy_answers_the_nix_handshake(tmp_path: Path) -> None:
    if shutil.which("nix-daemon") is None:
        pytest.skip("nix-daemon not available")
    sock_path = tmp_path / "socket"
    with nix_daemon_proxy(sock_path, [("max-jobs", "1")]):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(10)
            sock.connect(str(sock_path))
            sock.sendall(struct.pack("<Q", WORKER_MAGIC_1))
            reply = sock.recv(8)
        assert struct.unpack("<Q", reply)[0] == WORKER_MAGIC_2


def test_proxy_propagates_eof(tmp_path: Path) -> None:
    """Client half-close must reach the daemon, or every connection
    leaks a nix-daemon process that waits for more input."""
    if shutil.which("nix-daemon") is None:
        pytest.skip("nix-daemon not available")
    sock_path = tmp_path / "socket"
    with nix_daemon_proxy(sock_path, []), socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(10)
        sock.connect(str(sock_path))
        sock.sendall(struct.pack("<Q", WORKER_MAGIC_1))
        assert struct.unpack("<Q", sock.recv(8))[0] == WORKER_MAGIC_2
        sock.shutdown(socket.SHUT_WR)
        # Drain until EOF: only arrives if the daemon exited.
        while sock.recv(65536):
            pass
