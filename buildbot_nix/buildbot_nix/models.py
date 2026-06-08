"""Nix evaluation result models, ported from buildbot_nix.models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class CacheStatus(StrEnum):
    cached = "cached"
    local = "local"
    not_built = "notBuilt"


class NixEvalJobError(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    error: str
    attr: str
    attr_path: list[str] = Field(validation_alias="attrPath")


class NixEvalJobSuccess(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    attr: str
    attr_path: list[str] = Field(validation_alias="attrPath")
    cache_status: CacheStatus | None = Field(
        default=None, validation_alias="cacheStatus"
    )
    needed_builds: list[str] = Field(validation_alias="neededBuilds")
    needed_substitutes: list[str] = Field(validation_alias="neededSubstitutes")
    drv_path: str = Field(validation_alias="drvPath")
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
