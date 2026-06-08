"""Hercules-style secrets selection.

Effects declare the secrets they need via `secretsMap` in the
derivation environment; only those are exposed, renamed to their
destination names, after checking each secret's `condition` against
the run's context. Mirrors Hercules/Effect.hs and Hercules/Secrets.hs.

Divergences from hercules-ci-agent (both keep existing buildbot-nix
deployments working):
- an effect without secretsMap still gets the whole secrets file,
- a secret without a condition is allowed with a warning instead of
  being rejected.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any


class SecretsError(Exception):
    pass


@dataclass
class SecretContext:
    owner_name: str
    repo_name: str
    is_default_branch: bool
    ref: str


@dataclass
class SimpleSecret:
    name: str


@dataclass
class GitToken:
    pass


SecretRef = SimpleSecret | GitToken


def eval_condition(ctx: SecretContext, cond: Any) -> bool:  # noqa: ANN401, PLR0911
    """Hercules secret conditions: booleans, isDefaultBranch, isTag,
    {isBranch}, {isRepo}, {isOwner}, {and: [...]}, {or: [...]}."""
    if isinstance(cond, bool):
        return cond
    if cond == "isDefaultBranch":
        return ctx.is_default_branch
    if cond == "isTag":
        return ctx.ref.startswith("refs/tags/")
    if isinstance(cond, dict) and len(cond) == 1:
        [(key, value)] = cond.items()
        match key:
            case "and":
                return all(eval_condition(ctx, c) for c in value)
            case "or":
                return any(eval_condition(ctx, c) for c in value)
            case "isBranch":
                return ctx.ref == f"refs/heads/{value}"
            case "isRepo":
                return ctx.repo_name == value
            case "isOwner":
                return ctx.owner_name == value
    msg = f"unknown secret condition: {cond!r}"
    raise SecretsError(msg)


def parse_secrets_map(drv_env: dict[str, str]) -> dict[str, SecretRef]:
    """`secretsMap` (or legacy `secretsToUse`) from the derivation
    environment: destination name -> secret reference."""
    raw = drv_env.get("secretsToUse") or drv_env.get("secretsMap")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        msg = f"could not parse secretsMap in the derivation: {e}"
        raise SecretsError(msg) from e
    if not isinstance(parsed, dict):
        msg = (
            "secretsMap in the derivation must be a JSON object, "
            f"got {type(parsed).__name__}"
        )
        raise SecretsError(msg)
    refs: dict[str, SecretRef] = {}
    for dest, ref in parsed.items():
        if isinstance(ref, str):
            refs[dest] = SimpleSecret(ref)
        elif isinstance(ref, dict) and ref.get("type") == "GitToken":
            refs[dest] = GitToken()
        else:
            msg = (
                f"could not parse secret reference {dest!r}: "
                'must be a secret name or {"type": "GitToken"}'
            )
            raise SecretsError(msg)
    return refs


def gather_secrets(
    secrets_map: dict[str, SecretRef],
    all_secrets: dict[str, Any],
    ctx: SecretContext,
    git_token: str | None,
) -> dict[str, Any]:
    """The secrets.json content for one effect run."""
    out: dict[str, Any] = {}
    for dest, ref in secrets_map.items():
        match ref:
            case GitToken():
                if git_token is None:
                    msg = (
                        f"secret {dest!r} wants a GitToken but no forge "
                        "token is available for this run"
                    )
                    raise SecretsError(msg)
                out[dest] = {"data": {"token": git_token}}
            case SimpleSecret(name=name):
                secret = all_secrets.get(name)
                if secret is None:
                    msg = f"secret {name!r} (for {dest!r}) does not exist"
                    raise SecretsError(msg)
                condition = secret.get("condition")
                if condition is None:
                    print(
                        f"WARNING: secret {name!r} has no condition field; "
                        "hercules-ci-agent >= 0.9 would reject it",
                        file=sys.stderr,
                    )
                elif not eval_condition(ctx, condition):
                    msg = f"access to secret {name!r} denied by its condition"
                    raise SecretsError(msg)
                # The condition stays private to the runner.
                out[dest] = {"data": secret.get("data", {})}
    return out


def check_mounts(
    mountables: dict[str, Any],
    ctx: SecretContext,
    drv_mounts: dict[str, str],
) -> list[tuple[str, str, bool]]:
    """`__hci_effect_mounts` checked against the configured mountables
    allowlist; returns (container path, host path, read_only)."""
    mounts = []
    for path, name in drv_mounts.items():
        mountable = mountables.get(name)
        # Intentionally generic: misconfiguration details are sensitive.
        denied = SecretsError(
            f"mountable {name!r} is not configured or its condition "
            "does not allow this effect invocation"
        )
        if mountable is None:
            raise denied
        # Component-wise so dot-prefixed names like /srv/.config stay allowed.
        components = path.split("/")
        if (
            path == "/"
            or components[0] != ""
            or any(c in ("", ".", "..") for c in components[1:])
        ):
            msg = f"invalid mount path: {path!r}"
            raise SecretsError(msg)
        for protected in ("/nix", "/secrets", "/build", "/run"):
            if path == protected or path.startswith(protected + "/"):
                msg = f"mount over {protected} is not allowed: {path!r}"
                raise SecretsError(msg)
        if not eval_condition(ctx, mountable.get("condition", False)):
            raise denied
        mounts.append(
            (path, mountable["source"], bool(mountable.get("readOnly", True)))
        )
    return mounts
