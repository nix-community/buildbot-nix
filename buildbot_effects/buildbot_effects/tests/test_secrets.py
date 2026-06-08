from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from buildbot_effects import (
    BuildbotEffectsError,
    pass_as_file_env,
    sandbox_env,
    select_mounts,
    virtual_ids,
)
from buildbot_effects.options import EffectsOptions
from buildbot_effects.secrets import (
    GitToken,
    SecretContext,
    SecretsError,
    SimpleSecret,
    check_mounts,
    eval_condition,
    gather_secrets,
    parse_secrets_map,
)

CTX = SecretContext(
    owner_name="acme",
    repo_name="widget",
    is_default_branch=True,
    ref="refs/heads/main",
)


def test_eval_condition_primitives() -> None:
    assert eval_condition(CTX, True)  # noqa: FBT003 (condition JSON, not a flag)
    assert not eval_condition(CTX, False)  # noqa: FBT003
    assert eval_condition(CTX, "isDefaultBranch")
    assert not eval_condition(CTX, "isTag")
    tag_ctx = SecretContext(
        owner_name="acme",
        repo_name="widget",
        is_default_branch=False,
        ref="refs/tags/v1",
    )
    assert eval_condition(tag_ctx, "isTag")
    assert eval_condition(CTX, {"isBranch": "main"})
    assert not eval_condition(CTX, {"isBranch": "dev"})
    assert eval_condition(CTX, {"isRepo": "widget"})
    assert eval_condition(CTX, {"isOwner": "acme"})


def test_eval_condition_combinators() -> None:
    assert eval_condition(CTX, {"and": ["isDefaultBranch", {"isOwner": "acme"}]})
    assert not eval_condition(CTX, {"and": ["isDefaultBranch", "isTag"]})
    assert eval_condition(CTX, {"or": ["isTag", {"isRepo": "widget"}]})
    assert not eval_condition(CTX, {"or": []})
    assert eval_condition(CTX, {"and": []})
    with pytest.raises(SecretsError):
        eval_condition(CTX, {"frobnicate": 1})


def test_parse_secrets_map() -> None:
    env = {
        "secretsMap": json.dumps({"ssh": "deploy-key", "token": {"type": "GitToken"}})
    }
    refs = parse_secrets_map(env)
    assert refs == {"ssh": SimpleSecret("deploy-key"), "token": GitToken()}
    assert parse_secrets_map({}) == {}
    with pytest.raises(SecretsError):
        parse_secrets_map({"secretsMap": json.dumps({"x": {"type": "wat"}})})


def test_gather_secrets_filters_and_renames() -> None:
    all_secrets = {
        "deploy-key": {
            "data": {"key": "ssh-ed25519 ..."},
            "condition": {"and": ["isDefaultBranch", {"isOwner": "acme"}]},
        },
        "unrelated": {"data": {"x": 1}, "condition": True},
    }
    out = gather_secrets(
        {"ssh": SimpleSecret("deploy-key"), "token": GitToken()},
        all_secrets,
        CTX,
        git_token="forge-tok",  # noqa: S106
    )
    assert out == {
        "ssh": {"data": {"key": "ssh-ed25519 ..."}},
        "token": {"data": {"token": "forge-tok"}},
    }


def test_gather_secrets_denies_by_condition() -> None:
    all_secrets = {"k": {"data": {}, "condition": {"isOwner": "evil"}}}
    with pytest.raises(SecretsError, match="denied"):
        gather_secrets({"d": SimpleSecret("k")}, all_secrets, CTX, None)


def test_gather_secrets_missing() -> None:
    with pytest.raises(SecretsError, match="does not exist"):
        gather_secrets({"d": SimpleSecret("nope")}, {}, CTX, None)
    with pytest.raises(SecretsError, match="GitToken"):
        gather_secrets({"d": GitToken()}, {}, CTX, None)


def test_gather_secrets_no_condition_warns_but_allows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = gather_secrets(
        {"d": SimpleSecret("legacy")},
        {"legacy": {"data": {"v": 1}}},
        CTX,
        None,
    )
    assert out == {"d": {"data": {"v": 1}}}
    assert "no condition" in capsys.readouterr().err


def test_check_mounts() -> None:
    mountables = {
        "docker": {
            "source": "/var/run/docker.sock",
            "readOnly": False,
            "condition": {"isOwner": "acme"},
        },
        "denied": {"source": "/x", "readOnly": True, "condition": {"isOwner": "evil"}},
    }
    mounts = check_mounts(mountables, CTX, {"/var/run/docker.sock": "docker"})
    assert mounts == [("/var/run/docker.sock", "/var/run/docker.sock", False)]

    # dot-prefixed components are legitimate, only . and .. traversal is not
    mounts = check_mounts(mountables, CTX, {"/srv/.config": "docker"})
    assert mounts == [("/srv/.config", "/var/run/docker.sock", False)]

    for bad in (
        "relative",
        "/",
        "/nix/store",
        "/build/x",
        "/run",
        "/run/secrets.json",
        "/a/../b",
        "/a/./b",
        "/a//b",
        "/a/b/",
    ):
        with pytest.raises(SecretsError):
            check_mounts(mountables, CTX, {bad: "docker"})
    with pytest.raises(SecretsError):
        check_mounts(mountables, CTX, {"/y": "denied"})
    with pytest.raises(SecretsError):
        check_mounts(mountables, CTX, {"/y": "unknown"})


def test_home_is_not_inherited() -> None:
    """nix develop leaks the service user's HOME into the sandbox;
    hercules pins it to /homeless-shelter unless the derivation sets
    its own."""
    assert sandbox_env({})["HOME"] == "/homeless-shelter"
    assert sandbox_env({"HOME": "/build/home"})["HOME"] == "/build/home"


def test_pass_as_file_env(tmp_path: Path) -> None:
    drv_env = {
        "passAsFile": "buildCommand extra",
        "buildCommand": "echo hi",
        "extra": "x",
        "other": "kept",
    }
    env, clear = pass_as_file_env(drv_env, tmp_path)
    assert env == {
        "buildCommandPath": "/build/.attr-buildCommand",
        "extraPath": "/build/.attr-extra",
    }
    assert clear == {"buildCommand", "extra"}
    assert (tmp_path / ".attr-buildCommand").read_text() == "echo hi"
    assert (tmp_path / ".attr-extra").read_text() == "x"


def test_parse_secrets_map_non_object_json() -> None:
    """Valid JSON that is not an object must give a clean SecretsError."""
    for raw in ("[]", '"x"', "42"):
        with pytest.raises(SecretsError, match="must be a JSON object"):
            parse_secrets_map({"secretsMap": raw})


def test_select_mounts_malformed_json(tmp_path: Path) -> None:
    drv = {"env": {"__hci_effect_mounts": "{not json"}}
    with pytest.raises(SecretsError, match="__hci_effect_mounts"):
        select_mounts(drv, EffectsOptions(path=tmp_path))


def test_select_mounts_non_object_json(tmp_path: Path) -> None:
    drv = {"env": {"__hci_effect_mounts": "[1, 2]"}}
    with pytest.raises(SecretsError, match="__hci_effect_mounts"):
        select_mounts(drv, EffectsOptions(path=tmp_path))


def test_select_mounts_non_string_value(tmp_path: Path) -> None:
    drv = {"env": {"__hci_effect_mounts": '{"/mnt": ["x"]}'}}
    with pytest.raises(SecretsError, match="__hci_effect_mounts"):
        select_mounts(drv, EffectsOptions(path=tmp_path))


def test_virtual_ids_malformed() -> None:
    with pytest.raises(BuildbotEffectsError, match="__hci_effect_virtual_uid"):
        virtual_ids({"__hci_effect_virtual_uid": "not-a-number"})
    with pytest.raises(BuildbotEffectsError, match="__hci_effect_virtual_gid"):
        virtual_ids({"__hci_effect_virtual_gid": "1.5"})


def test_virtual_ids_defaults() -> None:
    assert virtual_ids({}) == (0, 0)
    assert virtual_ids({"__hci_effect_virtual_uid": "1000"}) == (1000, 1000)
