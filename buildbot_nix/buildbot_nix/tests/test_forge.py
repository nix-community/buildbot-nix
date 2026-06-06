"""Tests for forge clients (fake httpx transports), discovery filters,
and the project store incl. one-shot legacy topic import."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest

from buildbot_nix.config import RepoFilters
from buildbot_nix.forge import (
    DiscoveredRepo,
    GiteaClient,
    GitHubAppClient,
    GitHubFetchCredentialsProvider,
    filter_repos,
)
from buildbot_nix.gitea_hooks import (
    GiteaWebhookSecrets,
    register_repo_hook,
)
from buildbot_nix.migrations import apply_migrations
from buildbot_nix.projects import ProjectStore
from buildbot_nix.reconcile import gitea_heads, reconcile_project
from buildbot_nix.status import (
    GiteaStatusPoster,
    GitHubStatusPoster,
    StatusState,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


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


def github_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
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
                        # No admin permission: discovery must skip it
                        # (webhook registration would always fail).
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
        return httpx.Response(404)

    client = GiteaClient(
        "https://gitea.example.com",
        "tkn",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    repos = asyncio.run(client.discover_repos())
    assert len(repos) == 1
    assert repos[0].forge == "gitea"
    assert repos[0].forge_repo_id == "7"
    assert repos[0].topics == ("ci",)
    assert repos[0].private


# --- project store -----------------------------------------------------------------


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if shutil.which("initdb") is None:
        pytest.skip("postgresql not available")
    datadir = tmp_path_factory.mktemp("pgdata")
    sockdir = tmp_path_factory.mktemp("pgsock")
    subprocess.run(  # noqa: S603
        ["initdb", "-D", str(datadir), "-U", "test", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603
        ["postgres", "-D", str(datadir), "-k", str(sockdir), "-c", "listen_addresses="],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not Path(sockdir, ".s.PGSQL.5432").exists():
            if time.monotonic() > deadline:
                msg = "postgres did not start"
                raise RuntimeError(msg)
            time.sleep(0.1)
        subprocess.run(  # noqa: S603
            ["createdb", "-h", str(sockdir), "-U", "test", "forge"],
            check=True,
            capture_output=True,
        )
        dsn = f"postgresql://test@/forge?host={sockdir}"
        asyncio.run(apply_migrations(dsn))
        yield dsn
    finally:
        proc.terminate()
        proc.wait()


def test_project_store_sync_and_legacy_import(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            store = ProjectStore(pool)
            repos = [
                repo("acme", "tagged", topics=("build-with-buildbot",)),
                repo("acme", "untagged"),
            ]
            # First startup with empty table: topic import enables.
            await store.sync_discovered(
                repos, legacy_import_topic="build-with-buildbot"
            )
            enabled = await store.enabled_projects()
            assert [p.name for p in enabled] == ["tagged"]

            # Rename keeps identity and enablement (stable forge id).
            renamed = DiscoveredRepo(
                **{**repos[0].__dict__, "repo": "renamed", "topics": ()}
            )
            await store.sync_discovered(
                [renamed], legacy_import_topic="build-with-buildbot"
            )
            enabled = await store.enabled_projects()
            assert [p.name for p in enabled] == ["renamed"]

            # Non-empty table: topic import never runs again.
            newly_tagged = repo("acme", "later", topics=("build-with-buildbot",))
            await store.sync_discovered(
                [newly_tagged], legacy_import_topic="build-with-buildbot"
            )
            assert {p.name for p in await store.enabled_projects()} == {"renamed"}

            # Admin toggle.
            later = await store.by_forge_id("github", "acme-later")
            assert later is not None
            await store.set_enabled(later.id, enabled=True)
            assert {p.name for p in await store.enabled_projects()} == {
                "renamed",
                "later",
            }
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
            project_id = await pool.fetchval(
                """
                INSERT INTO projects (forge, forge_repo_id, owner, name,
                                      default_branch, url)
                VALUES ('gitea', 'hook-1', 'acme', 'widget', 'main', 'u')
                RETURNING id
                """
            )
            secrets_store = GiteaWebhookSecrets(pool)
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
            project_id = await pool.fetchval(
                """
                INSERT INTO projects (forge, forge_repo_id, owner, name,
                                      default_branch, url, enabled)
                VALUES ('gitea', 'recon-1', 'acme', 'recon', 'main', 'u', TRUE)
                RETURNING id
                """
            )
            # PR 6's head already has a build record.
            await pool.execute(
                """
                INSERT INTO builds (project_id, number, commit_sha, branch,
                                    status)
                VALUES ($1, 1, 'already-built', 'main', 'succeeded')
                """,
                project_id,
            )
            project = await ProjectStore(pool).by_forge_id("gitea", "recon-1")
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

            submitted = await reconcile_project(pool, project, heads, Sink())
            # main head + PR 5; PR 6 already built.
            assert submitted == 2
            shas = {e.commit_sha for e in events}  # type: ignore[attr-defined]
            assert shas == {"head-main", "head-pr5"}

            # First contact (no builds at all): default branch only, the
            # open-PR backlog is not built.
            await pool.execute("DELETE FROM builds WHERE project_id = $1", project_id)
            events.clear()
            submitted = await reconcile_project(pool, project, heads, Sink())
            assert submitted == 1
            assert events[0].commit_sha == "head-main"  # type: ignore[attr-defined]
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
            "nix-eval",
            StatusState.success,
            "evaluation succeeded",
            "https://ci.test/projects/acme/repo11/builds/1",
        )

    asyncio.run(run())
    assert posted == [
        {
            "state": "success",
            "context": "nix-eval",
            "description": "evaluation succeeded",
            "target_url": "https://ci.test/projects/acme/repo11/builds/1",
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
            "nix-build",
            StatusState.failure,
            "2 of 3 attributes failed",
            "https://ci.test/projects/acme/widget/builds/7",
        )
    )
    assert posted[0]["state"] == "failure"
    assert posted[0]["context"] == "nix-build"
