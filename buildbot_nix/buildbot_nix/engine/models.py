"""Nix evaluation result models, ported from buildbot_nix.models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, TypeAdapter


class CacheStatus(StrEnum):
    cached = "cached"
    local = "local"
    notBuilt = "notBuilt"  # noqa: N815


class NixEvalJobError(BaseModel):
    error: str
    attr: str
    attrPath: list[str]  # noqa: N815


class NixEvalJobSuccess(BaseModel):
    attr: str
    attrPath: list[str]  # noqa: N815
    cacheStatus: CacheStatus | None = None  # noqa: N815
    neededBuilds: list[str]  # noqa: N815
    neededSubstitutes: list[str]  # noqa: N815
    drvPath: str  # noqa: N815
    inputDrvs: dict[str, list[str]] | None = None  # noqa: N815
    name: str
    # nix-eval-jobs emits null output paths for impure and some
    # content-addressed derivations: it cannot know their store paths
    # without actually building them. Accept None so the whole eval step
    # does not crash; downstream consumers already treat a missing
    # out path as "not statically known".
    outputs: dict[str, str | None]
    system: str


NixEvalJob = NixEvalJobError | NixEvalJobSuccess
NixEvalJobModel: TypeAdapter[NixEvalJob] = TypeAdapter(NixEvalJob)


class NixDerivation(BaseModel):
    class InputDerivation(BaseModel):
        dynamicOutputs: dict[str, str]  # noqa: N815
        outputs: list[str]

    inputDrvs: dict[str, InputDerivation]  # noqa: N815
