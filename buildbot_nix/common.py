import contextlib
import http.client
import json
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


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
