"""Auto-create GitHub releases on merged PRs.

Research #3834 (release engineering with conventional commits): on
merge of a feat/<id> PR, walk the PR's commits, group by conventional-
commit type, decide a semver bump, tag the merge SHA, and post a
GitHub release with grouped changelog.

Bump rule (matches commitlint.next_bump):
  - any commit with breaking marker → major
  - any feat → minor
  - else if any fix/perf/refactor/security → patch
  - else nothing (chore-only PRs don't release)

Skips when the repo isn't ours, the merge SHA is empty, or
GITHUB_TOKEN is missing. Never raises into the webhook handler.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .commitlint import next_bump, parse, render_changelog
from .config import settings
from .logger import log

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _bump_version(latest: str, kind: str) -> str:
    m = _VERSION_RE.match(latest.strip())
    if not m:
        return "v0.1.0"  # nothing tagged yet → fresh start
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    if kind == "major":
        major += 1
        minor = 0
        patch = 0
    elif kind == "minor":
        minor += 1
        patch = 0
    elif kind == "patch":
        patch += 1
    else:
        return ""  # caller skips
    return f"v{major}.{minor}.{patch}"


async def auto_release(payload: dict[str, Any]) -> None:
    """Webhook entrypoint. Best-effort; logs on every branch."""
    token = settings.github_token
    if not token:
        log.info("release_bot_no_token")
        return
    repo_full = (payload.get("repository") or {}).get("full_name") or ""
    if "/" not in repo_full:
        return
    owner, repo = repo_full.split("/", 1)
    if not any(repo == r for r in settings.repo_paths):
        log.info("release_bot_skipped_foreign_repo", repo=repo)
        return
    pr = payload.get("pull_request") or {}
    merge_sha = pr.get("merge_commit_sha") or ""
    if not merge_sha:
        return
    pr_number = pr.get("number")
    api = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=15) as c:
        # Fetch PR commits.
        try:
            r = await c.get(f"{api}/pulls/{pr_number}/commits", headers=headers)
            r.raise_for_status()
            commits_raw = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("release_bot_commits_fetch_failed", err=str(e)[:200])
            return
        parsed = []
        for entry in commits_raw:
            msg = (entry.get("commit") or {}).get("message", "") or ""
            p = parse(msg)
            if p:
                parsed.append(p)
        bump = next_bump(parsed) if parsed else "none"
        if bump == "none":
            log.info("release_bot_skipped_no_release_commits", pr=pr_number)
            return
        # Find the latest tag.
        try:
            r = await c.get(f"{api}/tags?per_page=1", headers=headers)
            r.raise_for_status()
            tags = r.json() or []
            latest = tags[0]["name"] if tags else ""
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            latest = ""
        new_tag = _bump_version(latest, bump)
        if not new_tag:
            return
        body = render_changelog(parsed) or "(no user-facing changes)"
        # Create the release.
        try:
            r = await c.post(
                f"{api}/releases",
                headers=headers,
                json={
                    "tag_name": new_tag,
                    "target_commitish": merge_sha,
                    "name": f"{new_tag} — PR #{pr_number}",
                    "body": body,
                    "draft": False,
                    "prerelease": False,
                },
            )
        except httpx.HTTPError as e:
            log.warning("release_bot_create_failed", err=str(e)[:200])
            return
        if r.status_code in (200, 201):
            url = (r.json() or {}).get("html_url", "")
            log.info("release_bot_created", repo=repo, tag=new_tag, bump=bump, url=url)
        elif r.status_code == 422:
            # Tag already exists — common when re-firing the webhook.
            log.info("release_bot_tag_exists", repo=repo, tag=new_tag)
        else:
            log.warning("release_bot_unexpected", status=r.status_code, body=r.text[:200])
