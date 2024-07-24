import contextlib
import http.client
import json
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from buildbot.process.log import StreamLog
    from pydantic import BaseModel

from collections.abc import Generator

from buildbot.plugins import util
from buildbot.process.buildstep import BuildStep
from twisted.internet import defer, threads
from twisted.python.failure import Failure


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

    if not url.startswith("https:"):
        msg = "url must be https: {url}"
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


def filter_repos_by_topic(
    topic: str | None, repos: list[Y], topics: Callable[[Y], list[str]]
) -> list[Y]:
    return list(
        filter(
            lambda repo: topic is None or topic in topics(repo),
            repos,
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

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        d = threads.deferToThread(self.run_deferred)  # type: ignore[no-untyped-call]

        self.error_msg = ""

        def error_cb(failure: Failure) -> int:
            self.error_msg += failure.getTraceback()
            return util.FAILURE

        d.addCallbacks(lambda _: util.SUCCESS, error_cb)
        res = yield d
        if res == util.SUCCESS:
            return self.run_post()
        else:
            log: StreamLog = yield self.addLog("log")
            log.addStderr(f"Failed to reload project list: {self.error_msg}")
            return util.FAILURE


_T = TypeVar("_T", bound="BaseModel")


def model_validate_project_cache(cls: type[_T], project_cache_file: Path) -> list[_T]:
    return [
        cls.model_validate(data) for data in json.loads(project_cache_file.read_text())
    ]


def model_dump_project_cache(repos: list[_T]) -> str:
    return json.dumps([repo.model_dump() for repo in repos])


def filter_for_combined_builds(reports: Any) -> Any | None:
    properties = reports[0]["builds"][0]["properties"]

    if "report_status" in properties and not properties["report_status"][0]:
        return None
    return reports
