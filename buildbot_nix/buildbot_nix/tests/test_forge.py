"""Tests for forge clients (fake httpx transports), discovery filters,
and the project store incl. one-shot legacy topic import."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest

from buildbot_nix.config import RepoFilters
from buildbot_nix.forge import (
    DiscoveredRepo,
    ForgeError,
    GiteaClient,
    GitHubAppClient,
    GitHubFetchCredentialsProvider,
    GitlabClient,
    filter_repos,
)
from buildbot_nix.gitea_hooks import (
    register_repo_hook,
)
from buildbot_nix.gitlab_hooks import register_repo_hook as gitlab_register_repo_hook
from buildbot_nix.hook_secrets import WebhookSecrets
from buildbot_nix.reconcile import gitea_heads, gitlab_heads, reconcile_repo
from buildbot_nix.repos import RepoStore
from buildbot_nix.status import (
    GiteaStatusPoster,
    GitHubStatusPoster,
    StatusState,
)

from .support import insert_build, insert_project

if TYPE_CHECKING:
    from pathlib import Path


def repo(owner: str, name: str, topics: tuple[str, ...] = ()) -> DiscoveredRepo:
    return DiscoveredRepo(
        forge="github",
        forge_repo_id=f"{owner}-{name}",
        owner=owner,
        repo=name,
        default_branch="main",
        clone_url=f"https://github.com/{owner}/{name}.git",
        private=False,
        topics=topics,
    )


# --- filters ------------------------------------------------------------------


def test_filter_no_allowlists_allows_all() -> None:
    repos = [repo("a", "x"), repo("b", "y")]
    assert filter_repos(RepoFilters(), repos) == repos


def test_filter_user_allowlist() -> None:
    repos = [repo("a", "x"), repo("b", "y")]
    assert filter_repos(RepoFilters(user_allowlist=["a"]), repos) == [repos[0]]


def test_filter_repo_allowlist() -> None:
    repos = [repo("a", "x"), repo("b", "y")]
    assert filter_repos(RepoFilters(repo_allowlist=["b/y"]), repos) == [repos[1]]


def test_filter_union_of_allowlists() -> None:
    repos = [repo("a", "x"), repo("b", "y"), repo("c", "z")]
    filtered = filter_repos(
        RepoFilters(user_allowlist=["a"], repo_allowlist=["b/y"]), repos
    )
    assert filtered == [repos[0], repos[1]]


def test_topic_does_not_filter_discovery() -> None:
    # The topic only drives the one-shot legacy enablement import
    # (projects.py); it must not exclude repos from discovery.
    repos = [repo("a", "x", topics=("build-with-buildbot",)), repo("b", "y")]
    assert filter_repos(RepoFilters(topic="build-with-buildbot"), repos) == repos


# --- GitHub client ---------------------------------------------------------------


def github_transport(
    hook_url: str = "https://buildbot.example.com/webhooks/github",
    events: tuple[str, ...] = ("push", "pull_request"),
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"events": list(events)})
        if path == "/app/hook/config":
            return httpx.Response(200, json={"url": hook_url})
        if path == "/app/installations":
            return httpx.Response(200, json=[{"id": 11}, {"id": 22}])
        if path.startswith("/app/installations/") and path.endswith("/access_tokens"):
            inst = path.split("/")[3]
            return httpx.Response(201, json={"token": f"ghs_token_{inst}"})
        if path == "/installation/repositories":
            token = request.headers["Authorization"].removeprefix("Bearer ")
            inst = token.removeprefix("ghs_token_")
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": int(inst) * 100,
                            "name": f"repo{inst}",
                            "owner": {"login": "acme"},
                            "default_branch": "main",
                            "clone_url": f"https://github.com/acme/repo{inst}.git",
                            "private": inst == "22",
                            "topics": ["build-with-buildbot"],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def github_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitHubAppClient:
    key = tmp_path / "app-key.pem"
    subprocess.run(  # noqa: S603
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        capture_output=True,
    )
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    return GitHubAppClient(
        app_id=42,
        private_key_file=key,
        http=httpx.AsyncClient(transport=github_transport()),
    )


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_github_webhook_check_ok(github_client: GitHubAppClient) -> None:
    problems = asyncio.run(
        github_client.check_app_webhook("https://buildbot.example.com")
    )
    assert problems == []


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_github_webhook_check_misconfigured(
    github_client: GitHubAppClient,
) -> None:
    github_client.http = httpx.AsyncClient(
        transport=github_transport(hook_url="", events=("push",))
    )
    problems = asyncio.run(
        github_client.check_app_webhook("https://buildbot.example.com")
    )
    assert any("webhook URL" in p for p in problems)
    assert any("pull_request" in p for p in problems)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_github_discovery(github_client: GitHubAppClient) -> None:
    repos = asyncio.run(github_client.discover_repos())
    assert {r.name for r in repos} == {"acme/repo11", "acme/repo22"}
    assert {r.forge_repo_id for r in repos} == {"1100", "2200"}
    private = next(r for r in repos if r.name == "acme/repo22")
    assert private.private
    assert github_client.repo_installations == {
        "acme/repo11": 11,
        "acme/repo22": 22,
    }


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_github_fetch_credentials(github_client: GitHubAppClient) -> None:
    async def run() -> None:
        await github_client.discover_repos()
        provider = GitHubFetchCredentialsProvider(github_client)
        creds = await provider.get("https://github.com/acme/repo22.git")
        assert creds.netrc_file is not None
        content = creds.netrc_file.read_text()
        assert "x-access-token" in content
        assert "ghs_token_22" in content
        # Unknown repo: no credentials (public/netrc fallback).
        assert (await provider.get("https://github.com/other/x.git")).netrc_file is None

    asyncio.run(run())


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_github_fetch_credentials_enterprise_host(
    github_client: GitHubAppClient,
) -> None:
    # The netrc machine entry must match the host git fetches from.
    async def run() -> None:
        await github_client.discover_repos()
        provider = GitHubFetchCredentialsProvider(github_client)
        creds = await provider.get("https://ghe.example.com/acme/repo22.git")
        assert creds.netrc_file is not None
        assert "machine ghe.example.com " in creds.netrc_file.read_text()

    asyncio.run(run())


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_github_jwt_is_signed(github_client: GitHubAppClient) -> None:
    token = asyncio.run(github_client._app_jwt())  # noqa: SLF001
    header_b64, payload_b64, signature = token.split(".")

    def unpad(data: str) -> bytes:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    assert json.loads(unpad(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(unpad(payload_b64))
    assert payload["iss"] == "42"
    assert payload["exp"] > payload["iat"]
    assert signature


# --- Gitea client ---------------------------------------------------------------


def test_gitea_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.headers["Authorization"] == "token tkn"
        if path == "/api/v1/user/repos":
            page = int(request.url.params["page"])
            if page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "name": "widget",
                        "owner": {"login": "acme"},
                        "default_branch": "main",
                        "clone_url": "https://gitea.example.com/acme/widget.git",
                        "private": True,
                        # null permissions: allowed, must not crash.
                        "permissions": None,
                    },
                    {
                        # No admin permission: still discovered; hook
                        # registration degrades to a manual-setup hint.
                        "id": 8,
                        "name": "readonly",
                        "owner": {"login": "acme"},
                        "default_branch": "main",
                        "clone_url": "https://gitea.example.com/acme/readonly.git",
                        "private": False,
                        "permissions": {"admin": False},
                    },
                ],
            )
        if path == "/api/v1/repos/acme/widget/topics":
            return httpx.Response(200, json={"topics": ["ci"]})
        if path == "/api/v1/repos/acme/readonly/topics":
            return httpx.Response(200, json={"topics": []})
        return httpx.Response(404)

    client = GiteaClient(
        "https://gitea.example.com",
        "tkn",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    repos = asyncio.run(client.discover_repos())
    assert [r.forge_repo_id for r in repos] == ["7", "8"]
    assert repos[0].forge == "gitea"
    assert repos[0].topics == ("ci",)
    assert repos[0].private


# --- project store -----------------------------------------------------------------


def test_project_store_sync_and_legacy_import(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            store = RepoStore(pool)
            repos = [
                repo("acme", "tagged", topics=("build-with-buildbot",)),
                repo("acme", "untagged"),
            ]
            # First startup with empty table: topic import enables.
            await store.sync_discovered(
                repos, legacy_import_topic="build-with-buildbot"
            )
            enabled = await store.enabled_repos()
            assert [p.name for p in enabled] == ["tagged"]

            # Rename keeps identity and enablement (stable forge id).
            renamed = DiscoveredRepo(
                **{**repos[0].__dict__, "repo": "renamed", "topics": ()}
            )
            await store.sync_discovered(
                [renamed], legacy_import_topic="build-with-buildbot"
            )
            enabled = await store.enabled_repos()
            assert [p.name for p in enabled] == ["renamed"]

            # Non-empty table: topic import never runs again.
            newly_tagged = repo("acme", "later", topics=("build-with-buildbot",))
            await store.sync_discovered(
                [newly_tagged], legacy_import_topic="build-with-buildbot"
            )
            assert {p.name for p in await store.enabled_repos()} == {"renamed"}

            # Admin toggle.
            later = await store.by_forge_id("github", "acme-later")
            assert later is not None
            await store.set_enabled(later.id, enabled=True)
            assert {p.name for p in await store.enabled_repos()} == {
                "renamed",
                "later",
            }
        finally:
            await pool.close()

    asyncio.run(run())


def test_project_store_sync_skips_unchanged_rows(postgres_dsn: str) -> None:
    """Re-syncing identical repo metadata must not rewrite rows:
    discovery runs every poll cycle over every repo, and unconditional
    updates churn WAL and autovacuum on otherwise idle databases."""

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            store = RepoStore(pool)
            repos = [repo("acme", "stable"), repo("acme", "other")]
            await store.sync_discovered(repos)
            await store.sync_pull_based([("pull/one", "https://x/one.git", "main")])
            before = await pool.fetch(
                "SELECT name, xmin, updated_at FROM projects ORDER BY name"
            )

            await store.sync_discovered(repos)
            await store.sync_pull_based([("pull/one", "https://x/one.git", "main")])
            after = await pool.fetch(
                "SELECT name, xmin, updated_at FROM projects ORDER BY name"
            )
            assert [tuple(r) for r in before] == [tuple(r) for r in after]

            # A real change still updates the row.
            changed = DiscoveredRepo(
                **{**repos[0].__dict__, "default_branch": "develop"}
            )
            await store.sync_discovered([changed])
            row = await pool.fetchrow(
                "SELECT default_branch FROM projects WHERE name = 'stable'"
            )
            assert row is not None
            assert row["default_branch"] == "develop"
        finally:
            await pool.close()

    asyncio.run(run())


# --- gitea webhook auto-registration ------------------------------


def test_gitea_hook_registration(postgres_dsn: str) -> None:
    hooks = [
        {"id": 1, "config": {"url": "https://ci.example.com/change_hook/gitea"}},
        {
            "id": 2,
            "config": {"url": "https://other-ci.example.com/change_hook/gitea"},
        },
        # Trailing-slash legacy variant must be removed too.
        {"id": 3, "config": {"url": "https://ci.example.com/change_hook/gitea/"}},
    ]
    created: list[dict] = []
    deleted: list[str] = []
    patched: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/hooks"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=hooks if page == 1 else [])
        if request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        if request.method == "PATCH":
            patched.append((path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/hooks"):
            created.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(
                pool, forge="gitea", forge_repo_id="hook-1"
            )
            secrets_store = WebhookSecrets(pool, "gitea")
            client = GiteaClient(
                "https://gitea.example.com",
                "tkn",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            await register_repo_hook(
                client,
                secrets_store,
                project_id,
                "acme",
                "widget",
                "https://ci.example.com",
            )
            # Hook created with the stored per-repo secret.
            assert len(created) == 1
            hook = created[0]
            assert hook["config"]["url"] == "https://ci.example.com/webhooks/gitea"
            # PR head pushes arrive as pull_request_sync, not pull_request.
            assert hook["events"] == ["push", "pull_request", "pull_request_sync"]
            secret = await secrets_store.secret_for_repo("hook-1")
            assert hook["config"]["secret"] == secret
            # Legacy hooks removed only when they match OUR base URL,
            # with or without trailing slash.
            assert deleted == ["1", "3"]

            # Secret is stable across calls.
            assert await secrets_store.get_or_create(project_id) == secret

            # An existing hook is updated in place to re-sync the secret.
            hooks[:] = [
                {"id": 9, "config": {"url": "https://ci.example.com/webhooks/gitea"}}
            ]
            deleted.clear()
            await register_repo_hook(
                client,
                secrets_store,
                project_id,
                "acme",
                "widget",
                "https://ci.example.com",
            )
            assert len(created) == 1  # no duplicate hook created
            assert deleted == []
            assert len(patched) == 1
            hook_id, patch_body = patched[0]
            assert hook_id == "9"
            assert patch_body["config"]["secret"] == secret
        finally:
            await pool.close()

    asyncio.run(run())


# --- startup reconciliation ----------------------------------------


def test_gitlab_heads_carry_target_branch_base(postgres_dsn: str) -> None:
    """GitLab's MR API has no base sha; reconciliation must still merge
    MR heads into the target branch."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/projects/acme/glrecon/repository/branches/main":
            return httpx.Response(200, json={"commit": {"id": "head-main"}})
        if path == "/api/v4/projects/acme/glrecon/merge_requests":
            return httpx.Response(
                200,
                json=[
                    {
                        "iid": 5,
                        "sha": "head-mr5",
                        "target_branch": "main",
                        "author": {"username": "alice"},
                    }
                ],
            )
        return httpx.Response(404)

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            await insert_project(
                pool, "glrecon", forge="gitlab", forge_repo_id="glrecon-1"
            )
            project = await RepoStore(pool).by_forge_id("gitlab", "glrecon-1")
            assert project is not None
            client = GitlabClient(
                "https://gitlab.example.com",
                "tkn",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            heads = await gitlab_heads(client, project)
            mr_head = next(h for h in heads if h.pr_number == 5)
            assert mr_head.base_sha == "refs/heads/main"
        finally:
            await pool.close()

    asyncio.run(run())


def test_reconcile_unbuilt_heads(postgres_dsn: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/repos/acme/recon/branches/main":
            return httpx.Response(200, json={"commit": {"id": "head-main"}})
        if path == "/api/v1/repos/acme/recon/pulls":
            page = int(request.url.params.get("page", "1"))
            if page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 5,
                        "user": {"login": "alice"},
                        "head": {"sha": "head-pr5"},
                        "base": {"ref": "main", "sha": "base-pr5"},
                    },
                    {
                        "number": 6,
                        "user": {"login": "bob"},
                        "head": {"sha": "already-built"},
                        "base": {"ref": "main", "sha": "base-pr6"},
                    },
                ],
            )
        return httpx.Response(404)

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(
                pool, "recon", forge="gitea", forge_repo_id="recon-1"
            )
            # PR 6's head already has a build record.
            await insert_build(
                pool, project_id, commit_sha="already-built", status="succeeded"
            )
            project = await RepoStore(pool).by_forge_id("gitea", "recon-1")
            assert project is not None

            client = GiteaClient(
                "https://gitea.example.com",
                "tkn",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            heads = await gitea_heads(client, project)
            assert len(heads) == 3

            events: list[object] = []

            class Sink:
                async def submit(self, event: object) -> None:
                    events.append(event)

            submitted = await reconcile_repo(pool, project, heads, Sink())
            # main head + PR 5; PR 6 already built.
            assert submitted == 2
            shas = {e.commit_sha for e in events}  # type: ignore[attr-defined]
            assert shas == {"head-main", "head-pr5"}

            # First contact (no builds at all): default branch only, the
            # open-PR backlog is not built.
            await pool.execute("DELETE FROM builds WHERE project_id = $1", project_id)
            events.clear()
            submitted = await reconcile_repo(pool, project, heads, Sink())
            assert submitted == 1
            assert events[0].commit_sha == "head-main"  # type: ignore[attr-defined]

            # Cancelled-only history is still fresh: an operator who
            # cancels the initial build must not get the PR backlog on
            # the next restart.
            await insert_build(
                pool,
                project_id,
                number=2,
                commit_sha="head-main",
                status="cancelled",
            )
            events.clear()
            submitted = await reconcile_repo(pool, project, heads, Sink())
            assert submitted == 0  # main head cancelled, PRs skipped
        finally:
            await pool.close()

    asyncio.run(run())


# --- status posting against fake forge APIs -------------------------


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_github_status_post(github_client: GitHubAppClient) -> None:
    posted: list[dict] = []
    fallback = github_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo11/statuses/sha1":
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return fallback.handler(request)  # type: ignore[attr-defined,return-value]

    github_client.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=""
    )

    async def run() -> None:
        await github_client.discover_repos()
        poster = GitHubStatusPoster(github_client)
        await poster.post(
            "acme",
            "repo11",
            "sha1",
            "buildbot/nix-eval",
            StatusState.success,
            "evaluation succeeded",
            "https://ci.test/repos/acme/repo11/builds/1",
        )

    asyncio.run(run())
    assert posted == [
        {
            "state": "success",
            "context": "buildbot/nix-eval",
            "description": "evaluation succeeded",
            "target_url": "https://ci.test/repos/acme/repo11/builds/1",
        }
    ]


def test_gitea_status_post() -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/repos/acme/widget/statuses/sha9":
            assert request.headers["Authorization"] == "token tkn"
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)

    client = GiteaClient(
        "https://gitea.example.com",
        "tkn",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(
        GiteaStatusPoster(client).post(
            "acme",
            "widget",
            "sha9",
            "buildbot/nix-build",
            StatusState.failure,
            "2 of 3 attributes failed",
            "https://ci.test/repos/acme/widget/builds/7",
        )
    )
    assert posted[0]["state"] == "failure"
    assert posted[0]["context"] == "buildbot/nix-build"


def test_register_repo_hook_without_admin_warns(
    postgres_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """No admin permission on the repo: degrade to a manual-setup hint
    instead of a stack trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(403, json={"message": "forbidden"})
        return httpx.Response(404)

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(
                pool, "locked", forge="gitea", forge_repo_id="hook-403"
            )
            client = GiteaClient(
                "https://gitea.example.com",
                "tkn",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            await register_repo_hook(
                client,
                WebhookSecrets(pool, "gitea"),
                project_id,
                "acme",
                "locked",
                "https://ci.example.com",
            )
        finally:
            await pool.close()

    with caplog.at_level("WARNING"):
        asyncio.run(run())
    assert any("manually" in r.message for r in caplog.records)


def test_gitlab_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == "glpat-x"
        if request.url.path == "/api/v4/projects":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "path_with_namespace": "group/sub/tool",
                        "default_branch": "develop",
                        "http_url_to_repo": "https://gitlab.example.com/group/sub/tool.git",
                        "visibility": "private",
                        "topics": ["ci"],
                    },
                    {
                        "id": 8,
                        "path_with_namespace": "Mic92/dotfiles",
                        "default_branch": "main",
                        "http_url_to_repo": "https://gitlab.example.com/Mic92/dotfiles.git",
                        "visibility": "public",
                    },
                ],
            )
        raise AssertionError(request.url.path)

    client = GitlabClient(
        "https://gitlab.example.com/",
        "glpat-x",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    nested, public = asyncio.run(client.discover_repos())
    assert nested == DiscoveredRepo(
        forge="gitlab",
        forge_repo_id="7",
        owner="group/sub",
        repo="tool",
        default_branch="develop",
        clone_url="https://gitlab.example.com/group/sub/tool.git",
        private=True,
        topics=("ci",),
    )
    assert public.name == "Mic92/dotfiles"
    assert not public.private
    assert (
        client.project_api_url("group/sub", "tool")
        == "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Ftool"
    )


def test_gitlab_hook_registration(postgres_dsn: str) -> None:
    hooks: list[dict] = []
    created: list[dict] = []
    updated: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.startswith(b"/api/v4/projects/acme%2Fwidget/hooks")
        if request.method == "GET":
            return httpx.Response(200, json=hooks)
        if request.method == "POST":
            created.append(json.loads(request.content))
            return httpx.Response(201, json={})
        if request.method == "PUT":
            updated.append(
                (request.url.path.rsplit("/", 1)[-1], json.loads(request.content))
            )
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(
                pool, forge="gitlab", forge_repo_id="glhook-1"
            )
            secrets_store = WebhookSecrets(pool, "gitlab")
            client = GitlabClient(
                "https://gitlab.example.com",
                "tkn",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            await gitlab_register_repo_hook(
                client,
                secrets_store,
                project_id,
                "acme",
                "widget",
                "https://ci.example.com",
            )
            assert len(created) == 1
            hook = created[0]
            assert hook["url"] == "https://ci.example.com/webhooks/gitlab"
            assert hook["push_events"]
            assert hook["merge_requests_events"]
            assert hook["token"] == await secrets_store.secret_for_repo("glhook-1")

            # An existing hook is updated in place to re-sync the secret.
            hooks[:] = [{"id": 9, "url": "https://ci.example.com/webhooks/gitlab"}]
            await gitlab_register_repo_hook(
                client,
                secrets_store,
                project_id,
                "acme",
                "widget",
                "https://ci.example.com",
            )
            assert len(created) == 1
            assert updated[0][0] == "9"
        finally:
            await pool.close()

    asyncio.run(run())


def test_register_repo_hook_500_with_403_in_body_raises(postgres_dsn: str) -> None:
    """A 500 whose response body merely contains "403" is not a
    permission problem: it must propagate, not degrade to a warning."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(500, json={"message": "object id 403 missing"})
        return httpx.Response(404)

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(
                pool, "err500", forge="gitea", forge_repo_id="hook-500"
            )
            client = GiteaClient(
                "https://gitea.example.com",
                "tkn",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            with pytest.raises(ForgeError):
                await register_repo_hook(
                    client,
                    WebhookSecrets(pool, "gitea"),
                    project_id,
                    "acme",
                    "err500",
                    "https://ci.example.com",
                )
        finally:
            await pool.close()

    asyncio.run(run())


def test_gitlab_register_repo_hook_status_classification(
    postgres_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """403 degrades to a manual-setup warning; a 500 whose body
    contains "403" propagates as ForgeError."""
    status = 403

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(status, json={"message": "see id 403"})
        return httpx.Response(404)

    async def run() -> None:
        nonlocal status
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(
                pool, "glperm", forge="gitlab", forge_repo_id="glhook-403"
            )
            client = GitlabClient(
                "https://gitlab.example.com",
                "tkn",
                http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            secrets_store = WebhookSecrets(pool, "gitlab")
            await gitlab_register_repo_hook(
                client,
                secrets_store,
                project_id,
                "acme",
                "glperm",
                "https://ci.example.com",
            )
            status = 500
            with pytest.raises(ForgeError):
                await gitlab_register_repo_hook(
                    client,
                    secrets_store,
                    project_id,
                    "acme",
                    "glperm",
                    "https://ci.example.com",
                )
        finally:
            await pool.close()

    with caplog.at_level("WARNING"):
        asyncio.run(run())
    assert any("manually" in r.message for r in caplog.records)
