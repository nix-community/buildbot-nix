"""Authorization utilities for buildbot-nix."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from buildbot.plugins import util
from buildbot.www.authz.endpointmatchers import EndpointMatcherBase, Match
from twisted.logger import Logger

if TYPE_CHECKING:
    from buildbot.www.authz import Authz

    from .projects import GitBackend, GitProject

log = Logger()


def normalize_virtual_builder_name(name: str) -> str:
    if re.match(r"^[^:]+:", name) is not None:
        # rewrites github:nix-community/srvos#checks.aarch64-linux.nixos-stable-example-hardware-hetzner-online-intel -> nix-community/srvos/nix-build
        match = re.match(r"[^:]+:(?P<owner>[^/]+)/(?P<repo>[^#]+)#.+", name)
        if match:
            return f"{match['owner']}/{match['repo']}/nix-build"

    return name


class AnyProjectEndpointMatcher(EndpointMatcherBase):
    def __init__(self, builders: set[str] | None = None, **kwargs: Any) -> None:
        if builders is None:
            builders = set()
        self.builders = builders
        super().__init__(**kwargs)

    async def check_builder(
        self,
        endpoint_object: Any,
        endpoint_dict: dict[str, Any],
        object_type: str,
    ) -> Match | None:
        res = await endpoint_object.get({}, endpoint_dict)
        if res is None:
            return None

        builderid = res.get("builderid")
        if builderid is None:
            builder_name = res["builder_names"][0]
        else:
            builder = await self.master.data.get(("builders", builderid))
            builder_name = builder["name"]

        builder_name = normalize_virtual_builder_name(builder_name)
        if builder_name in self.builders:
            log.warn(
                "Builder {builder} allowed by {role}: {builders}",
                builder=builder_name,
                role=self.role,
                builders=self.builders,
            )
            return Match(self.master, **{object_type: res})
        log.warn(
            "Builder {builder} not allowed by {role}: {builders}",
            builder=builder_name,
            role=self.role,
            builders=self.builders,
        )
        return None

    async def match_ForceSchedulerEndpoint_force(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        _options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildEndpoint_rebuild(  # noqa: N802
        self, epobject: Any, epdict: dict[str, Any], _options: dict[str, Any]
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        _options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildRequestEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        _options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "buildrequest")


def setup_authz(
    backends: list[GitBackend],
    projects: list[GitProject],
    admins: list[str],
    *,
    allow_unauthenticated_control: bool = False,
) -> Authz:
    allow_rules = []

    # When enabled, permit all control actions without authentication
    if allow_unauthenticated_control:
        allow_rules.append(util.AnyEndpointMatcher(role="", defaultDeny=False))
        return util.Authz(
            roleMatchers=[],
            allowRules=allow_rules,
        )

    allowed_builders_by_org: defaultdict[str, set[str]] = defaultdict(
        lambda: {backend.reload_builder_name for backend in backends},
    )

    for project in projects:
        if project.belongs_to_org:
            for builder in ["nix-build", "nix-eval"]:
                allowed_builders_by_org[project.owner].add(f"{project.name}/{builder}")

    for org, allowed_builders in allowed_builders_by_org.items():
        allow_rules.append(
            AnyProjectEndpointMatcher(
                builders=allowed_builders,
                role=org,
                defaultDeny=False,
            ),
        )

    allow_rules.append(util.AnyEndpointMatcher(role="admin", defaultDeny=False))
    allow_rules.append(util.AnyControlEndpointMatcher(role="admins"))
    return util.Authz(
        roleMatchers=[
            util.RolesFromUsername(roles=["admin"], usernames=admins),
            util.RolesFromGroups(groupPrefix=""),  # so we can match on ORG
        ],
        allowRules=allow_rules,
    )
