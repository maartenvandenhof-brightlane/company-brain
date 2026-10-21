"""Minimal git wrapper. Commands are argv lists (never a shell) and hooks are disabled."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RECORD = "\x1e"
_FIELD = "\x1f"


class GitError(RuntimeError):
    pass


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    body: str
    files: list[tuple[str, str]] = field(default_factory=list)  # (status, path)


class GitRepo:
    def __init__(
        self,
        root: Path,
        *,
        committer_name: str = "wiki-mcp",
        committer_email: str = "wiki-mcp@localhost",
    ) -> None:
        self.root = root
        self.committer_name = committer_name
        self.committer_email = committer_email

    # ------------------------------------------------------------------ plumbing

    def _run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            "git",
            "-c", f"safe.directory={self.root}",
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false",
            "-c", "core.quotepath=off",
            "-C", str(self.root),
            *args,
        ]  # fmt: skip
        full_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - git needs a HOME
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C.UTF-8",
            "GIT_COMMITTER_NAME": self.committer_name,
            "GIT_COMMITTER_EMAIL": self.committer_email,
            **(env or {}),
        }
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, env=full_env, timeout=timeout, check=False
        )
        if check and result.returncode != 0:
            raise GitError(
                f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    # ------------------------------------------------------------------ setup

    def is_repo(self) -> bool:
        return (self.root / ".git").is_dir()

    def init(self, branch: str = "main") -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._run("init", "-q", "-b", branch)

    def head(self) -> str | None:
        result = self._run("rev-parse", "--verify", "-q", "HEAD", check=False)
        sha = result.stdout.strip()
        return sha if _SHA.match(sha) else None

    # ------------------------------------------------------------------ writes

    def commit(
        self,
        paths: list[PurePosixPath],
        message: str,
        *,
        author_name: str,
        author_email: str,
    ) -> str:
        """Stage exactly `paths` (additions, modifications or deletions) and commit."""
        if not paths:
            raise GitError("nothing to commit")
        str_paths = [str(p) for p in paths]
        self._run("add", "-A", "--", *str_paths)
        env = {"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email}
        self._run("commit", "-q", "--no-verify", "-m", message, "--", *str_paths, env=env)
        head = self.head()
        if head is None:
            raise GitError("commit did not produce a HEAD")
        return head

    def commit_all(self, message: str, *, author_name: str, author_email: str) -> str:
        self._run("add", "-A")
        env = {"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email}
        self._run("commit", "-q", "--no-verify", "--allow-empty", "-m", message, env=env)
        head = self.head()
        assert head is not None
        return head

    def restore(self, paths: list[PurePosixPath]) -> None:
        """Undo uncommitted changes to `paths`: tracked files are reset, new files removed."""
        for p in paths:
            tracked = self._run("cat-file", "-e", f"HEAD:{p}", check=False)
            self._run("reset", "-q", "--", str(p), check=False)
            if tracked.returncode == 0:
                self._run("checkout", "-q", "--", str(p), check=False)
            else:
                (self.root / str(p)).unlink(missing_ok=True)

    def is_dirty(self) -> bool:
        return bool(self._run("status", "--porcelain").stdout.strip())

    # ------------------------------------------------------------------ history

    def log(self, *, since: str | None = None, limit: int = 20) -> list[Commit]:
        args = [
            "log",
            f"-n{max(1, min(limit, 500))}",
            "--name-status",
            f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%aI{_FIELD}%s{_FIELD}%b{_FIELD}",
        ]
        if since:
            args.append(f"--since={since}")
        if self.head() is None:
            return []
        out = self._run(*args).stdout
        commits: list[Commit] = []
        for record in out.split(_RECORD)[1:]:
            sha, author, date, subject, body, rest = record.split(_FIELD, 5)
            files = []
            for line in rest.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    files.append((parts[0][0], parts[-1]))
            commits.append(Commit(sha, author, date, subject, body.strip(), files))
        return commits

    def first_added(self, path: PurePosixPath, text: str) -> str | None:
        """Commit date (ISO) of the first commit whose `path` gained `text`, if any."""
        if self.head() is None:
            return None
        out = self._run(
            "log", "--reverse", "--format=%cI", f"-S{text}", "--", str(path)
        ).stdout.split()
        return out[0] if out else None

    def last_commit(self, path: PurePosixPath) -> str | None:
        """The sha of the latest commit that touched `path`, if any."""
        if self.head() is None:
            return None
        sha = self._run("log", "-1", "--format=%H", "--", str(path)).stdout.strip()
        return sha if _SHA.match(sha) else None

    def committed_text(self, path: PurePosixPath) -> str | None:
        """The current committed file, used to ensure merge originals exist in Git history."""
        result = self._run("show", f"HEAD:{path}", check=False)
        return result.stdout if result.returncode == 0 else None

    def renamed_to(self, path: PurePosixPath) -> set[str]:
        """Every path git history shows `path` renamed to (rename detection, whole history)."""
        if self.head() is None:
            return set()
        out = self._run("log", "-M", "--diff-filter=R", "--name-status", "--format=").stdout
        found = set()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].startswith("R") and parts[1] == str(path):
                found.add(parts[2])
        return found

    def subject(self, sha: str) -> str:
        return self._run("log", "-1", "--format=%s", sha).stdout.strip()

    def commits_with_trailer(self, trailer: str) -> list[str]:
        """Shas of the commits whose message has the line `trailer`, newest first."""
        if self.head() is None:
            return []
        pattern = "^" + re.escape(trailer) + "$"
        out = self._run("log", "--format=%H", "-E", f"--grep={pattern}").stdout
        return [s for s in out.split() if _SHA.match(s)]

    # ------------------------------------------------------------------ revert

    def revert_start(self, sha: str) -> list[str]:
        """Apply the inverse of commit `sha` to the index and working tree without committing;
        the paths left in conflict (empty when it applied cleanly)."""
        if not _SHA.match(sha):
            raise GitError("not a commit sha")
        self._run("revert", "--no-commit", "--no-edit", sha, check=False)
        out = self._run("diff", "--name-only", "--diff-filter=U").stdout
        return [p for p in out.splitlines() if p]

    def revert_abort(self) -> None:
        self._run("revert", "--abort", check=False)

    def revert_finish(self, keep_head: list[PurePosixPath]) -> list[str]:
        """End a started revert without committing it: `keep_head` paths go back to HEAD (they
        are regenerated), the rest stays changed in the working tree; the changed paths."""
        if keep_head:
            self._run("checkout", "HEAD", "--", *[str(p) for p in keep_head])
        self._run("revert", "--quit", check=False)
        out = self._run("diff", "--cached", "--name-only").stdout
        # Unstaged again: the working tree holds the reverted state, and committing the paths
        # stages them (a page the commit created is then a deletion git add can match).
        self._run("reset", "-q")
        return [p for p in out.splitlines() if p]

    # ------------------------------------------------------------------ remote

    def push(self, remote_url: str, branch: str, *, ssh_key: Path | None = None) -> None:
        env = {}
        if ssh_key is not None:
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {ssh_key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
                "-o BatchMode=yes"
            )
        self._run("push", "-q", remote_url, f"HEAD:refs/heads/{branch}", env=env, timeout=120)
