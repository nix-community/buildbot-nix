import contextlib
import http.client
import json
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from twisted.python import log


class GithubError(Exception):
    pass


class HttpResponse:
    def __init__(self, raw: http.client.HTTPResponse) -> None:
        self.raw = raw

    def json(self) -> Any:
        return json.load(self.raw)

    def headers(self) -> http.client.HTTPMessage:
        return self.raw.headers


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
        raise GithubError(msg)

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
        raise GithubError(msg) from e
    return HttpResponse(resp)


def paginated_github_request(url: str, token: str) -> list[dict[str, Any]]:
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
            raise GithubError(msg) from e
        next_url = None
        link = res.headers()["Link"]
        if link is not None:
            links = link.split(", ")
            for link in links:  # pagination
                link_parts = link.split(";")
                if link_parts[1].strip() == 'rel="next"':
                    next_url = link_parts[0][1:-1]
        items += res.json()
    return items


def slugify_project_name(name: str) -> str:
    return name.replace(".", "-").replace("/", "-")


class GithubProject:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    @property
    def repo(self) -> str:
        return self.data["name"]

    @property
    def owner(self) -> str:
        return self.data["owner"]["login"]

    @property
    def name(self) -> str:
        return self.data["full_name"]

    @property
    def url(self) -> str:
        return self.data["html_url"]

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data["full_name"])

    @property
    def default_branch(self) -> str:
        return self.data["default_branch"]

    @property
    def topics(self) -> list[str]:
        return self.data["topics"]

    @property
    def belongs_to_org(self) -> bool:
        return self.data["owner"]["type"] == "Organization"


def create_project_hook(
    owner: str,
    repo: str,
    token: str,
    webhook_url: str,
    webhook_secret: str,
) -> None:
    hooks = paginated_github_request(
        f"https://api.github.com/repos/{owner}/{repo}/hooks?per_page=100",
        token,
    )
    config = dict(
        url=webhook_url,
        content_type="json",
        insecure_ssl="0",
        secret=webhook_secret,
    )
    data = dict(name="web", active=True, events=["push", "pull_request"], config=config)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for hook in hooks:
        if hook["config"]["url"] == webhook_url:
            log.msg(f"hook for {owner}/{repo} already exists")
            return

    http_request(
        f"https://api.github.com/repos/{owner}/{repo}/hooks",
        method="POST",
        headers=headers,
        data=data,
    )


def refresh_projects(github_token: str, repo_cache_file: Path) -> None:
    repos = []

    for repo in paginated_github_request(
        "https://api.github.com/user/repos?per_page=100",
        github_token,
    ):
        if not repo["permissions"]["admin"]:
            name = repo["full_name"]
            log.msg(
                f"skipping {name} because we do not have admin privileges, needed for hook management",
            )
        else:
            repos.append(repo)

    with NamedTemporaryFile("w", delete=False, dir=repo_cache_file.parent) as f:
        path = Path(f.name)
        try:
            f.write(json.dumps(repos))
            f.flush()
            path.rename(repo_cache_file)
        except OSError:
            path.unlink()
            raise


def load_projects(github_token: str, repo_cache_file: Path) -> list[GithubProject]:
    if not repo_cache_file.exists():
        return []

    repos: list[dict[str, Any]] = sorted(
        json.loads(repo_cache_file.read_text()), key=lambda x: x["full_name"]
    )
    return [GithubProject(repo) for repo in repos]
