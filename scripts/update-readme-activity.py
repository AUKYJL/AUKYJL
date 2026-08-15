#!/usr/bin/env python3
"""Refresh the README activity strip from public GitHub data."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


README_DEFAULT = Path(__file__).resolve().parents[1] / "README.md"
BEGIN_MARKER = "<!-- MAGAZINE:BEGIN ACTIVITY -->"
END_MARKER = "<!-- MAGAZINE:END ACTIVITY -->"
API_BASE = "https://api.github.com"
USERNAME = "AUKYJL"
DEFAULT_ROWS = 5
COMMITS_PER_REPO = 3
REQUEST_TIMEOUT = 30
HEADER_LINE = '<img src="assets/magazine-activity-heading-dark.svg" alt="Editorial heading for recent activity." width="100%">'


@dataclass(frozen=True)
class ActivityRow:
    date: str
    action: str
    repo: str
    short_sha: str
    subject: str
    sort_key: datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print the generated block without modifying the README.")
    parser.add_argument(
        "--readme",
        default=str(README_DEFAULT),
        help="Path to the README file to update. Defaults to the repository README.",
    )
    return parser.parse_args()


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "aukyjl-readme-activity-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_json(path: str) -> object:
    url = f"{API_BASE}{path}"
    request = urllib.request.Request(url, headers=github_headers())
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.load(response)


def parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def clean_subject(value: str) -> str:
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    return " ".join(first_line.split())


def repo_slug(full_name: str) -> str:
    return full_name.rsplit("/", 1)[-1]


def row_from_event(event: dict[str, object]) -> list[ActivityRow]:
    event_type = str(event.get("type") or "")
    repo_name = repo_slug(str((event.get("repo") or {}).get("name") or ""))
    created_at = str(event.get("created_at") or "")
    payload = event.get("payload") or {}

    if not repo_name or not created_at or not isinstance(payload, dict):
        return []

    timestamp = parse_iso8601(created_at)
    date = timestamp.date().isoformat()

    if event_type == "PushEvent":
        rows = []
        commits = payload.get("commits") or []
        if not isinstance(commits, list):
            return []
        for commit in reversed(commits):
            if not isinstance(commit, dict):
                continue
            sha = str(commit.get("sha") or "")[:7]
            subject = clean_subject(str(commit.get("message") or ""))
            if not sha or not subject:
                continue
            rows.append(ActivityRow(date=date, action="PUSH", repo=repo_name, short_sha=sha, subject=subject, sort_key=timestamp))
        return rows

    subject = ""
    action = ""

    if event_type == "CreateEvent":
        action = "CREATE"
        ref_type = str(payload.get("ref_type") or "").strip()
        ref = str(payload.get("ref") or "").strip()
        if ref_type == "repository":
            subject = "repository"
        elif ref_type and ref:
            subject = f"{ref_type} {ref}"
        else:
            subject = ref_type or "create"
    elif event_type == "DeleteEvent":
        action = "DELETE"
        ref_type = str(payload.get("ref_type") or "").strip()
        ref = str(payload.get("ref") or "").strip()
        if ref_type and ref:
            subject = f"{ref_type} {ref}"
        else:
            subject = clean_subject(ref_type or ref or "delete")
    elif event_type == "ReleaseEvent":
        action = "RELEASE"
        release = payload.get("release") or {}
        if isinstance(release, dict):
            subject = clean_subject(str(release.get("tag_name") or release.get("name") or "release"))
    elif event_type == "ForkEvent":
        action = "FORK"
        forkee = payload.get("forkee") or {}
        if isinstance(forkee, dict):
            subject = clean_subject(str(forkee.get("full_name") or "fork"))
    elif event_type == "WatchEvent":
        action = "STAR"
        subject = "starred repository"
    elif event_type == "PublicEvent":
        action = "PUBLIC"
        subject = "made repository public"
    elif event_type == "IssuesEvent":
        action = "ISSUE"
        issue = payload.get("issue") or {}
        if isinstance(issue, dict):
            issue_number = issue.get("number")
            issue_title = clean_subject(str(issue.get("title") or "issue"))
            event_action = clean_subject(str(payload.get("action") or "updated")).upper()
            subject = f"{event_action} #{issue_number} {issue_title}".strip()
    elif event_type == "PullRequestEvent":
        action = "PR"
        pr = payload.get("pull_request") or {}
        if isinstance(pr, dict):
            pr_number = pr.get("number")
            pr_title = clean_subject(str(pr.get("title") or "pull request"))
            event_action = clean_subject(str(payload.get("action") or "updated")).upper()
            subject = f"{event_action} #{pr_number} {pr_title}".strip()

    subject = clean_subject(subject)
    if not action or not subject:
        return []
    return [ActivityRow(date=date, action=action, repo=repo_name, short_sha="", subject=subject, sort_key=timestamp)]


def collect_event_rows(limit: int) -> list[ActivityRow]:
    events = fetch_json(f"/users/{urllib.parse.quote(USERNAME)}/events/public?per_page=100")
    if not isinstance(events, list):
        return []

    rows: list[ActivityRow] = []
    seen: set[tuple[str, str, str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        for row in row_from_event(event):
            dedupe_key = (row.action, row.repo, row.short_sha, row.subject)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows


def collect_commit_rows(limit: int) -> list[ActivityRow]:
    repos = fetch_json(f"/users/{urllib.parse.quote(USERNAME)}/repos?type=owner&sort=pushed&per_page=100")
    if not isinstance(repos, list):
        return []

    rows: list[ActivityRow] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        if repo.get("private"):
            continue
        full_name = str(repo.get("full_name") or "")
        default_branch = str(repo.get("default_branch") or "")
        if not full_name or not default_branch:
            continue

        path = f"/repos/{full_name}/commits?per_page={COMMITS_PER_REPO}&sha={urllib.parse.quote(default_branch)}"
        try:
            commits = fetch_json(path)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                continue
            raise

        if not isinstance(commits, list):
            continue

        for commit in commits:
            if not isinstance(commit, dict):
                continue
            metadata = commit.get("commit") or {}
            if not isinstance(metadata, dict):
                continue
            committer = metadata.get("committer") or {}
            author = metadata.get("author") or {}
            if not isinstance(committer, dict) or not isinstance(author, dict):
                continue
            timestamp_raw = str(committer.get("date") or author.get("date") or "")
            subject = clean_subject(str(metadata.get("message") or ""))
            sha = str(commit.get("sha") or "")[:7]
            if not timestamp_raw or not subject or not sha:
                continue
            timestamp = parse_iso8601(timestamp_raw)
            rows.append(
                ActivityRow(
                    date=timestamp.date().isoformat(),
                    action="COMMIT",
                    repo=repo_slug(full_name),
                    short_sha=sha,
                    subject=subject,
                    sort_key=timestamp,
                )
            )

    rows.sort(key=lambda row: row.sort_key, reverse=True)

    deduped: list[ActivityRow] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        dedupe_key = (row.repo, row.short_sha)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(row)
        if len(deduped) >= limit:
            return deduped
    return deduped


def build_activity_rows(limit: int = DEFAULT_ROWS) -> tuple[list[ActivityRow], str]:
    event_rows = collect_event_rows(limit)
    if event_rows:
        return event_rows[:limit], "events"

    commit_rows = collect_commit_rows(limit)
    return commit_rows[:limit], "commits"


def render_block(rows: list[ActivityRow]) -> str:
    action_width = max(6, *(len(row.action) for row in rows)) if rows else 6
    repo_width = max(10, *(len(row.repo) for row in rows)) if rows else 10
    sha_width = 7

    lines = []
    for row in rows:
        lines.append(
            f"{row.date}  {row.action:<{action_width}}  {row.repo:<{repo_width}}  {row.short_sha:<{sha_width}}  {row.subject}"
        )

    code_block = "\n".join(lines) if lines else "no public activity available"
    return f"{HEADER_LINE}\n\n```text\n{code_block}\n```"


def replace_activity_block(readme_text: str, rendered_block: str) -> str:
    start = readme_text.find(BEGIN_MARKER)
    end = readme_text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("Could not find ACTIVITY markers in README.")

    start_content = start + len(BEGIN_MARKER)
    if start_content < len(readme_text) and readme_text[start_content] == "\n":
        start_content += 1

    content = f"{rendered_block.rstrip()}\n"
    return readme_text[:start_content] + content + readme_text[end:]


def main() -> int:
    args = parse_args()
    readme_path = Path(args.readme).resolve()
    readme_text = readme_path.read_text(encoding="utf-8")

    rows, source = build_activity_rows(DEFAULT_ROWS)
    rendered_block = render_block(rows)
    updated_text = replace_activity_block(readme_text, rendered_block)
    changed = updated_text != readme_text

    if args.dry_run:
        print(rendered_block)
        print()
        print(f"source: {source}")
        print(f"readme_would_change: {'yes' if changed else 'no'}")
        return 0

    if changed:
        readme_path.write_text(updated_text, encoding="utf-8")
        print(f"Updated {readme_path} from {source} data.")
    else:
        print(f"{readme_path} is already up to date ({source}).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(f"GitHub API request failed: HTTP {exc.code} {exc.reason}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"GitHub API request failed: {exc.reason}", file=sys.stderr)
        raise SystemExit(1)
