"""Tests for the proxy/TLS/NIX_* environment passthrough helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nixbot.environ import passthrough_env

if TYPE_CHECKING:
    import pytest


def test_passthrough_env_includes_proxy_tls_and_nix_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("https_proxy", "http://proxy:3128")
    monkeypatch.setenv("NO_PROXY", "internal.example.com")
    monkeypatch.setenv("NIX_SSL_CERT_FILE", "/etc/ssl/ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/ca.pem")
    monkeypatch.setenv("NIX_CONFIG", "substituters = https://cache.example.com")
    env = passthrough_env()
    assert env["https_proxy"] == "http://proxy:3128"
    assert env["NO_PROXY"] == "internal.example.com"
    assert env["NIX_SSL_CERT_FILE"] == "/etc/ssl/ca.pem"
    assert env["SSL_CERT_FILE"] == "/etc/ssl/ca.pem"
    assert env["NIX_CONFIG"] == "substituters = https://cache.example.com"


def test_passthrough_env_excludes_unrelated_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    monkeypatch.delenv("NIX_CONFIG", raising=False)
    env = passthrough_env()
    assert "HOME" not in env
    assert "CREDENTIALS_DIRECTORY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
