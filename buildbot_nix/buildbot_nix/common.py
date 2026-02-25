from __future__ import annotations

import contextlib
import json
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    import http.client
    from collections.abc import Callable

    from pydantic import BaseModel
    from twisted.python.failure import Failure

    from .models import RepoFilters

from buildbot.plugins import util
from buildbot.process.buildstep import BuildStep
from buildbot.util.twisted import async_to_deferred
from twisted.internet import threads


def slugify_project_name(name: str) -> str:
    return name.replace(".", "-").replace("/", "-")


def paginated_github_request(
    url: str, token: str, subkey: None | str = None
) -> list[dict[str, Any]]:
    next_url: str | None = url
    items = []
    while next_url:
        try:
            res = http_request(
                next_url,
                headers={"Authorization": f"Bearer {token}"},
            )
        except OSError as e:
            msg = f"failed to fetch {next_url}: {e}"
            raise HttpError(msg) from e
        next_url = None
        link = res.headers()["Link"]
        if link is not None:
            links = link.split(", ")
            for link in links:  # pagination
                link_parts = link.split(";")
                if link_parts[1].strip() == 'rel="next"':
                    next_url = link_parts[0][1:-1]
        if subkey is not None:
            items += res.json()[subkey]
        else:
            items += res.json()
    return items


class HttpResponse:
    def __init__(self, raw: http.client.HTTPResponse) -> None:
        self.raw = raw

    def json(self) -> Any:
        return json.load(self.raw)

    def headers(self) -> http.client.HTTPMessage:
        return self.raw.headers


class HttpError(Exception):
    pass


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
) -> HttpResponse:
    body = None
    if data:
        body = json.dumps(data).encode("ascii")
    if headers is None:
        headers = {}
    headers = headers.copy()
    headers["User-Agent"] = "buildbot-nix"

    if not (url.startswith(("https:", "http:"))):
        msg = f"url must be http or https: {url}"
        raise HttpError(msg)

    req = urllib.request.Request(  # noqa: S310
        url, headers=headers, method=method, data=body
    )
    try:
        resp = urllib.request.urlopen(req)  # noqa: S310
    except urllib.request.HTTPError as e:
        resp_body = ""
        with contextlib.suppress(OSError, UnicodeDecodeError):
            resp_body = e.fp.read().decode("utf-8", "replace")
        msg = f"Request for {method} {url} failed with {e.code} {e.reason}: {resp_body}"
        raise HttpError(msg) from e
    return HttpResponse(resp)


def atomic_write_file(file: Path, data: str) -> None:
    with NamedTemporaryFile("w", delete=False, dir=file.parent) as f:
        path = Path(f.name)
        try:
            f.write(data)
            f.flush()
            path.rename(file)
        except OSError:
            path.unlink()
            raise


Y = TypeVar("Y")


T = TypeVar("T")


@dataclass
class RepoAccessors[T]:
    repo_name: Callable[[T], str]
    user: Callable[[T], str]
    topics: Callable[[T], list[str]]


def filter_repos(
    filters: RepoFilters,
    repos: list[Y],
    accessors: RepoAccessors[Y],
) -> list[Y]:
    # This is a bit complicated so let me explain:
    # If both `user_allowlist` and `repo_allowlist` are `None` then we want to allow everything,
    # however if either are non-`None`, then the one that is non-`None` determines whether to
    # allow a repo, or both if both are non-+None`.

    return list(
        filter(
            lambda repo: (
                (filters.user_allowlist is None and filters.repo_allowlist is None)
                or (
                    filters.user_allowlist is not None
                    and accessors.user(repo) in filters.user_allowlist
                )
                or (
                    filters.repo_allowlist is not None
                    and accessors.repo_name(repo) in filters.repo_allowlist
                )
            ),
            filter(
                lambda repo: (
                    filters.topic is None or filters.topic in accessors.topics(repo)
                ),
                repos,
            ),
        )
    )


class ThreadDeferredBuildStep(BuildStep, ABC):
    def __init__(
        self,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

    @abstractmethod
    def run_deferred(self) -> None:
        pass

    @abstractmethod
    def run_post(self) -> Any:
        pass

    @async_to_deferred
    async def run(self) -> int:
        d = threads.deferToThread(self.run_deferred)  # type: ignore[no-untyped-call]

        self.error_msg = ""

        def error_cb(failure: Failure) -> int:
            self.error_msg += failure.getTraceback()
            return util.FAILURE

        d.addCallbacks(lambda _: util.SUCCESS, error_cb)
        res = await d
        if res == util.SUCCESS:
            return self.run_post()
        log = await self.addLog("log")
        log.addStderr(f"Failed to reload project list: {self.error_msg}")  # type: ignore[attr-defined]
        return util.FAILURE


def model_validate_project_cache[T: BaseModel](
    cls: type[T], project_cache_file: Path
) -> list[T]:
    return [
        cls.model_validate(data) for data in json.loads(project_cache_file.read_text())
    ]


def model_dump_project_cache[T: BaseModel](repos: list[T]) -> str:
    return json.dumps([repo.model_dump() for repo in repos])


def filter_for_combined_builds(reports: Any) -> Any | None:
    properties = reports[0]["builds"][0]["properties"]

    if "report_status" in properties and not properties["report_status"][0]:
        return None
    return reports
