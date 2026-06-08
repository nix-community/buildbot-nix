"""Environment passthrough for spawned tools (nix-eval-jobs, git).

Subprocesses run with scrubbed environments for hygiene, but proxy
settings, custom CA bundles and NIX_* configuration must still reach
them: otherwise evaluation and fetching fail in proxy/custom-CA
deployments while everything else works.
"""

from __future__ import annotations

import os

# Proxy and TLS trust configuration honored by curl/libcurl and nix.
_PASSTHROUGH_VARS = frozenset(
    {
        "http_proxy",
        "https_proxy",
        "ftp_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "FTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "NIX_SSL_CERT_FILE",
    }
)


def passthrough_env() -> dict[str, str]:
    """Proxy, TLS CA and NIX_* variables from the service environment,
    for layering under a subprocess's explicit environment."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _PASSTHROUGH_VARS or key.startswith("NIX_")
    }
