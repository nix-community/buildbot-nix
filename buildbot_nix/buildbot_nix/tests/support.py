"""Shared test helpers."""

from __future__ import annotations

from buildbot_nix.models import CacheStatus, NixEvalJobSuccess


def mk_job(
    attr: str = "foo",
    *,
    deps: list[str] | None = None,
    cache_status: CacheStatus = CacheStatus.not_built,
    system: str = "x86_64-linux",
    out: str | None = None,
) -> NixEvalJobSuccess:
    """An eval result like nix-eval-jobs emits: needed_builds covers
    the job's own drv plus its dependencies."""
    drv = f"/nix/store/{attr}.drv"
    return NixEvalJobSuccess(
        attr=attr,
        attr_path=attr.split("."),
        cache_status=cache_status,
        needed_builds=[drv, *(f"/nix/store/{d}.drv" for d in (deps or []))],
        needed_substitutes=[],
        drv_path=drv,
        name=attr,
        outputs={"out": out or f"/nix/store/{attr}-out"},
        system=system,
    )


def cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    """Cookies as a request header (per-request cookies= is
    deprecated in httpx)."""
    return {"cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
