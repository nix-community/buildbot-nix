"""Git clone and worktree management.

One central persistent bare clone per project; each build gets its own
git worktree from that clone so concurrent builds of the same project
never re-fetch. Worktrees are removed after the build. Clones are a
cache: on corruption they are deleted and re-cloned. A per-project lock
serializes fetches; `git worktree prune` plus an orphan sweep runs at
startup and periodically, as does `git gc`.

PR builds merge the PR head into the base branch locally in the
worktree; a conflict is a failed build. Build identity is the
post-merge tree hash (`HEAD^{tree}`).

Fetch credentials come from a provider interface. The static/netrc
implementation covers public repos and operator-supplied netrc;
GitHub App per-fetch installation tokens plug in via forge integration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class GitError(Exception):
    """A git command failed."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr}")


class MergeConflictError(Exception):
    """Local merge of the PR head into the base branch conflicted."""


@dataclass(frozen=True)
class FetchCredentials:
    """Credentials for fetching a repository.

    `netrc_file` is bind-mounted/read for the duration of one fetch or
    eval only; it must be scoped to the repository being fetched.
    SSH key/known-hosts files cover per-repo SSH fetch (pull-based
    repos and the gitea.sshPrivateKeyFile option).
    """

    netrc_file: Path | None = None
    # Raw forge token for hercules GitToken secret references.
    token: str | None = None
    ssh_private_key_file: Path | None = None
    ssh_known_hosts_file: Path | None = None

    def git_ssh_command(self) -> str | None:
        if self.ssh_private_key_file is None and self.ssh_known_hosts_file is None:
            return None
        parts = ["ssh", "-o", "BatchMode=yes"]
        if self.ssh_private_key_file is not None:
            parts += ["-i", str(self.ssh_private_key_file)]
        if self.ssh_known_hosts_file is not None:
            parts += [
                "-o",
                f"UserKnownHostsFile={self.ssh_known_hosts_file}",
            ]
        return " ".join(parts)


class CredentialsProvider(Protocol):
    """Provides per-fetch credentials for a repository URL."""

    async def get(self, repo_url: str) -> FetchCredentials: ...


class StaticCredentialsProvider:
    """Static provider: a fixed netrc file (or nothing, for public repos)."""

    def __init__(self, netrc_file: Path | None = None) -> None:
        self.netrc_file = netrc_file

    async def get(self, repo_url: str) -> FetchCredentials:  # noqa: ARG002
        return FetchCredentials(netrc_file=self.netrc_file)


async def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    credentials: FetchCredentials | None = None,
) -> str:
    """Run a git command, returning stdout. Raises GitError on failure."""
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    # Pass through environment-based git config (GIT_CONFIG_COUNT and
    # friends), e.g. for protocol.file.allow.
    for key, value in os.environ.items():
        if key.startswith("GIT_CONFIG_") and key not in env:
            env[key] = value
    if credentials is not None:
        ssh_command = credentials.git_ssh_command()
        if ssh_command is not None:
            env["GIT_SSH_COMMAND"] = ssh_command
    if credentials is not None and credentials.netrc_file is not None:
        # git's libcurl reads $HOME/.netrc (CURL_NETRC_OPTIONAL); point
        # HOME at a throwaway directory containing only the scoped netrc.
        home = Path(tempfile.mkdtemp(prefix="git-netrc-"))
        (home / ".netrc").symlink_to(credentials.netrc_file)
        env["HOME"] = str(home)
    else:
        home = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise GitError(args, proc.returncode or -1, stderr.decode(errors="replace"))
        return stdout.decode(errors="replace")
    finally:
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)


def _worktree_paths(porcelain_output: str) -> set[Path]:
    """Worktree paths from `git worktree list --porcelain`, resolved
    because git reports symlink-resolved paths."""
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in porcelain_output.splitlines()
        if line.startswith("worktree ")
    }


@dataclass
class Worktree:
    """A per-build checkout backed by a project's central clone."""

    path: Path
    clone_path: Path

    async def rev_parse(self, rev: str) -> str:
        return (await run_git(["rev-parse", rev], cwd=self.path)).strip()

    async def tree_hash(self) -> str:
        """Post-merge tree hash: the build identity."""
        return await self.rev_parse("HEAD^{tree}")

    async def commit_message(self, rev: str = "HEAD") -> str:
        return await run_git(["log", "-1", "--format=%B", rev], cwd=self.path)

    async def merge(self, head_sha: str) -> None:
        """Merge `head_sha` into the currently checked-out base branch.

        Raises MergeConflictError on conflict; the caller fails the
        build and reports the status on the head SHA.
        """
        try:
            await run_git(
                [
                    "-c",
                    "user.name=buildbot-nix",
                    "-c",
                    "user.email=buildbot-nix@localhost",
                    "merge",
                    "--no-ff",
                    "-m",
                    f"merge {head_sha} into base",
                    head_sha,
                ],
                cwd=self.path,
            )
        except GitError as e:
            # Only a genuine content conflict (unmerged index entries)
            # is a permanent MergeConflictError; everything else
            # (index.lock contention, missing objects, disk full) must
            # stay a GitError so callers treat it as transient/infra.
            conflicted = False
            with contextlib.suppress(GitError):
                unmerged = await run_git(["ls-files", "--unmerged"], cwd=self.path)
                conflicted = bool(unmerged.strip())
            with contextlib.suppress(GitError):
                await run_git(["merge", "--abort"], cwd=self.path)
            if not conflicted:
                raise
            msg = f"merge of {head_sha} conflicts with base branch: {e.stderr}"
            raise MergeConflictError(msg) from e


class RepoManager:
    """Manages central per-project clones and per-build worktrees."""

    def __init__(self, state_dir: Path) -> None:
        self.clones_dir = state_dir / "clones"
        self.worktrees_dir = state_dir / "worktrees"
        self.clones_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._fetch_locks: dict[str, asyncio.Lock] = {}

    def clone_path(self, project_key: str) -> Path:
        # project_key like "github/owner/repo"; one directory per project.
        return self.clones_dir / project_key / "clone.git"

    def _lock(self, project_key: str) -> asyncio.Lock:
        return self._fetch_locks.setdefault(project_key, asyncio.Lock())

    async def fetch(
        self,
        project_key: str,
        url: str,
        refspecs: list[str],
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Clone or update the central clone, serialized per project.

        On git corruption the clone is deleted and re-created (clones
        are a cache).
        """
        async with self._lock(project_key):
            path = self.clone_path(project_key)
            try:
                await self._fetch_once(path, url, refspecs, credentials)
            except GitError as e:
                # GitError also covers transient network/auth failures;
                # only delete the clone (which backs live builds'
                # worktrees) when it is actually corrupted.
                if await self._clone_healthy(path):
                    raise
                logger.warning(
                    "clone corrupted, re-cloning",
                    extra={"project": project_key, "stderr": e.stderr},
                )
                shutil.rmtree(path, ignore_errors=True)
                await self._fetch_once(path, url, refspecs, credentials)

    @staticmethod
    async def _clone_healthy(path: Path) -> bool:
        """Cheap local corruption check: HEAD's commit object must be
        readable from the object store."""
        if not (path / "HEAD").exists():
            return False
        try:
            await run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path)
        except GitError:
            return False
        return True

    async def _fetch_once(
        self,
        path: Path,
        url: str,
        refspecs: list[str],
        credentials: FetchCredentials | None,
    ) -> None:
        if not (path / "HEAD").exists():
            shutil.rmtree(path, ignore_errors=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            await run_git(["clone", "--bare", url, str(path)], credentials=credentials)
        await run_git(
            ["fetch", "--force", "--prune", url, *refspecs],
            cwd=path,
            credentials=credentials,
        )

    async def show_file(self, project_key: str, ref: str, file_path: str) -> str | None:
        """Read one file from a ref of the bare clone (no worktree).
        Returns None when the ref or file does not exist."""
        try:
            return await run_git(
                ["show", f"{ref}:{file_path}"], cwd=self.clone_path(project_key)
            )
        except GitError:
            return None

    async def create_worktree(
        self, project_key: str, worktree_id: str, commit: str
    ) -> Worktree:
        """Create a detached worktree for one build at `commit`."""
        clone = self.clone_path(project_key)
        path = self.worktrees_dir / worktree_id
        if path.exists():
            await self.remove_worktree(Worktree(path=path, clone_path=clone))
        path.parent.mkdir(parents=True, exist_ok=True)
        await run_git(["worktree", "add", "--detach", str(path), commit], cwd=clone)
        return Worktree(path=path, clone_path=clone)

    async def remove_worktree(self, worktree: Worktree) -> None:
        try:
            await run_git(
                ["worktree", "remove", "--force", str(worktree.path)],
                cwd=worktree.clone_path,
            )
        except GitError:
            shutil.rmtree(worktree.path, ignore_errors=True)
            with contextlib.suppress(GitError):
                await run_git(["worktree", "prune"], cwd=worktree.clone_path)

    async def cleanup(self) -> None:
        """Prune stale worktrees and sweep orphans; run at startup and
        periodically."""
        # Snapshot candidates before scanning the clones: a worktree
        # created mid-scan would be missing from `registered` and must
        # not become a sweep candidate.
        candidates = {entry.resolve() for entry in self.worktrees_dir.iterdir()}
        registered: set[Path] = set()
        for clone in self.clones_dir.rglob("clone.git"):
            with contextlib.suppress(GitError):
                await run_git(["worktree", "prune"], cwd=clone)
                output = await run_git(["worktree", "list", "--porcelain"], cwd=clone)
                registered.update(_worktree_paths(output))
        # Orphan sweep: worktree directories no clone knows about.
        for entry in candidates - registered:
            logger.info("removing orphan worktree", extra={"path": str(entry)})
            shutil.rmtree(entry, ignore_errors=True)

    async def gc(self) -> None:
        """Periodic `git gc` over all clones."""
        for clone in self.clones_dir.rglob("clone.git"):
            with contextlib.suppress(GitError):
                await run_git(["gc", "--auto"], cwd=clone)

    async def checkout_for_build(
        self,
        project_key: str,
        worktree_id: str,
        *,
        base_commit: str,
        head_commit: str | None = None,
        credentials: FetchCredentials | None = None,
    ) -> Worktree:
        """Create the build worktree: base commit, optionally merging a
        PR head into it. Submodules are checked out recursively (the
        bare clone does not carry submodule objects). Returns the
        worktree; identity is its tree hash."""
        worktree = await self.create_worktree(project_key, worktree_id, base_commit)
        try:
            if head_commit is not None and head_commit != base_commit:
                await worktree.merge(head_commit)
            if (worktree.path / ".gitmodules").exists():
                await run_git(
                    ["submodule", "update", "--init", "--recursive"],
                    cwd=worktree.path,
                    credentials=credentials,
                )
        except BaseException:
            # Callers only remove worktrees they received; a failed
            # merge or submodule checkout (or cancellation) must not
            # leak a registered worktree.
            await self.remove_worktree(worktree)
            raise
        return worktree
