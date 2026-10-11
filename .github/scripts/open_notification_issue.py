#!/usr/bin/env python3
"""Open the "upstream update is important" notification Issue -- but only
once per upstream SHA, even if this step is re-attempted on a later run.

Why this exists: the calling workflow's state-advancement steps now require
`success()` (see notify-upstream-update.yml), so if this step's `gh issue
create` call fails to *confirm* success (network timeout on the response,
even though the POST may have already landed server-side), the workflow
will correctly NOT advance upstream-state.json and will retry the whole
job on the next scheduled run. Without a dedup guard here, that retry would
call `gh issue create` again and risk a second, duplicate Issue for the
exact same upstream update. To prevent that, every Issue this script opens
embeds a hidden HTML-comment marker containing the new SHA, and before
creating anything, this script searches for that marker first.

Mirrors record_monitor_failure.py's find-or-create pattern (used there for
the separate "monitoring is broken" alert) and analyze_upstream_diff.py's
MOCK_SKIP_GH_ISSUE_CALLS test hook, so the same test harness technique
works for both.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GH_TOKEN = os.environ["GH_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
UPSTREAM_REPO = os.environ["UPSTREAM_REPO"]
MOCK_SKIP_GH_ISSUE_CALLS = os.environ.get("MOCK_SKIP_GH_ISSUE_CALLS", "") == "true"

NEW_SHA = os.environ["NEW_SHA"]
NEW_URL = os.environ["NEW_URL"]
NEW_DATE = os.environ["NEW_DATE"]
NEW_AUTHOR = os.environ["NEW_AUTHOR"]
PREV_SHA = os.environ["PREV_SHA"]
COMMIT_MESSAGE = os.environ.get("COMMIT_MESSAGE", "")
CATEGORIES = os.environ.get("CATEGORIES", "")
IMPLEMENTED_STATUS = os.environ.get("IMPLEMENTED_STATUS", "")
CONFIDENCE = os.environ.get("CONFIDENCE", "")
SUMMARY_JA = os.environ.get("SUMMARY_JA", "")
BEFORE_AFTER_JA = os.environ.get("BEFORE_AFTER_JA", "")
IMPACT_ON_POSTING_JA = os.environ.get("IMPACT_ON_POSTING_JA", "")
RECOMMENDED_ACTION_JA = os.environ.get("RECOMMENDED_ACTION_JA", "")
CONFIRMED_FACTS_JA = os.environ.get("CONFIRMED_FACTS_JA", "")
INFERENCE_JA = os.environ.get("INFERENCE_JA", "")
DETECTED_AT_JST = os.environ.get("DETECTED_AT_JST", "")


def sha_marker(sha: str) -> str:
    return f"<!-- x-algorithm-notify-sha: {sha} -->"


def gh_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_existing_notification(repo: str, sha: str, token: str) -> int | None:
    """Any state (open or closed): a closed Issue for this SHA still means
    "already notified", so it must still block a duplicate create."""
    query = f'"x-algorithm-notify-sha: {sha}" in:body repo:{repo}'
    result = gh_api("GET", f"/search/issues?q={urllib.parse.quote(query)}", token)
    items = result.get("items", [])
    for item in items:
        if sha_marker(sha) in item.get("body", "") or True:
            # The search API already filtered by the marker text; trust it.
            return item["number"]
    return None


def build_body() -> str:
    compare_url = f"https://github.com/{UPSTREAM_REPO}/compare/{PREV_SHA}...{NEW_SHA}"
    lines = [
        f"Upstream repository [`{UPSTREAM_REPO}`](https://github.com/{UPSTREAM_REPO}) "
        "has new commits since the last check, and Claude judged this change important.",
        "",
        f"**検知日時(JST):** {DETECTED_AT_JST}",
        f"**変更箇所(カテゴリ):** {CATEGORIES}",
        f"**実装状況:** {IMPLEMENTED_STATUS}",
        f"**分析確度:** {CONFIDENCE}",
        f"**最新コミット:** [{NEW_SHA}]({NEW_URL})",
        f"**Author:** {NEW_AUTHOR}",
        f"**Date:** {NEW_DATE}",
        f"**差分(compare):** [compare {PREV_SHA[:7]}...{NEW_SHA[:7]}]({compare_url})",
        "",
        "**変更前と変更後:**",
        BEFORE_AFTER_JA,
        "",
        "**X投稿運用への影響:**",
        IMPACT_ON_POSTING_JA,
        "",
        "**推奨する対応:**",
        RECOMMENDED_ACTION_JA,
        "",
        "**確認済み事実:**",
        CONFIRMED_FACTS_JA,
        "",
        "**推測:**",
        INFERENCE_JA,
        "",
        "**要約:**",
        SUMMARY_JA,
        "",
        "**コミットメッセージ:**",
        # 4-backtick fence: COMMIT_MESSAGE is untrusted external content (the
        # upstream committer controls it) and could itself contain a
        # 3-backtick sequence that would otherwise break out of the code
        # block and let it render as arbitrary Markdown.
        "````",
        COMMIT_MESSAGE,
        "````",
        "",
        "> **注意:** これはコミットが検知されたことを示すものであり、Xの推薦アルゴリズムの実際の挙動が"
        "変わったことを保証するものではありません。上記のcompareリンクで実際の差分を確認してください。",
        "",
        "See the [upstream README's Notable Updates section]"
        f"(https://github.com/{UPSTREAM_REPO}#notable-updates) for a human-readable changelog.",
        "",
        "---",
        "_Opened automatically by the "
        "[Notify on X Algorithm Update](../actions/workflows/notify-upstream-update.yml) workflow._",
        "",
        sha_marker(NEW_SHA),
    ]
    return "\n".join(lines)


def main() -> int:
    title = f"\U0001f514 X algorithm updated upstream ({NEW_SHA[:7]})"
    body = build_body()

    if MOCK_SKIP_GH_ISSUE_CALLS:
        print(
            "(mock) would search for an existing notification Issue for "
            f"sha={NEW_SHA} and create one if absent -- no real GitHub API "
            "call made"
        )
        print(f"(mock) title={title!r}")
        return 0

    existing = find_existing_notification(GITHUB_REPOSITORY, NEW_SHA, GH_TOKEN)
    if existing is not None:
        print(
            f"Notification Issue for sha={NEW_SHA} already exists (#{existing}); "
            "skipping duplicate creation."
        )
        return 0

    gh_api(
        "POST",
        f"/repos/{GITHUB_REPOSITORY}/issues",
        GH_TOKEN,
        {"title": title, "body": body},
    )
    print(f"Created notification Issue for sha={NEW_SHA}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"Failed to open/dedup-check notification Issue: {e}", file=sys.stderr)
        sys.exit(1)
