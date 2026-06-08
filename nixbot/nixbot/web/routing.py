"""Custom Starlette path convertor for project owners.

GitLab nested groups put "/" into the owner (e.g. "group/sub"), which
plain `{owner}` parameters cannot match. The `owner` convertor accepts
one or more path segments, but never segments that start a fixed route
suffix (builds/rows/attrs/schedules); otherwise a multi-segment owner
would swallow `/builds/{number}` and misroute deeper URLs to the repo
page. The `segment` convertor is the single-segment variant for the
project name: without it, `/repos/f/a/b/rows` would parse as
owner="a/b", name="rows" on the repo route. Repos literally named
after a reserved word are not routable; the forges reserve these names
themselves (GitLab) or they are vanishingly unlikely.
"""

from __future__ import annotations

from starlette.convertors import Convertor, register_url_convertor

# Literal segments that follow {name} in project routes.
_RESERVED = ("builds", "rows", "attrs", "schedules")
_SEGMENT = r"(?:(?!(?:" + "|".join(_RESERVED) + r")(?:/|$))[^/]+)"


class OwnerConvertor(Convertor[str]):
    regex = rf"{_SEGMENT}(?:/{_SEGMENT})*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


class SegmentConvertor(OwnerConvertor):
    regex = _SEGMENT


def register_owner_convertor() -> None:
    """Idempotent; must run before any route using `{owner:owner}` or
    `{name:segment}`."""
    register_url_convertor("owner", OwnerConvertor())
    register_url_convertor("segment", SegmentConvertor())
