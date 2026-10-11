#!/usr/bin/env python3
"""Record a Claude-analysis failure in the health-state file and, once a
consecutive-failure threshold is crossed, create or update a dedicated
alert Issue so the failure is visible without checking the Actions tab.

Run only when the "Analyze diff with Claude" step failed
(`if: steps.analyze.outcome == 'failure'`). Does not touch
.github/upstream-state.json or any Issue created for an actual upstream
update; this is a separate, independent commit/alert path.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HEALTH_STATE_FILE = os.environ.get("HEALTH_STATE_FILE", ".github/monitor-health-state.json")
ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "3"))
GH_TOKEN = os.environ["GH_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]  # owner/repo, set by GitHub Actions
ALERT_TITLE = "⚠️ 監視システム異常: Claude分析が連続失敗しています"
RUN_URL = (
    f"https://github.com/{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
)


def load_health_state(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "consecutive_claude_failures": 0,
            "calls_this_month": 0,
            "budget_month": "",
            "last_success_at": None,
            "last_failure_at": None,
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_health_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: {e.code} {detail}") from e


def find_open_alert_issue(repo: str, token: str) -> int | None:
    query = f'"{ALERT_TITLE}" in:title repo:{repo} is:issue is:open'
    result = gh_api(
        "GET",
        f"/search/issues?q={urllib.parse.quote(query)}",
        token,
    )
    items = result.get("items", [])
    for item in items:
        if item.get("title") == ALERT_TITLE:
            return item["number"]
    return None


def main() -> int:
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = load_health_state(HEALTH_STATE_FILE)
    state["consecutive_claude_failures"] = state.get("consecutive_claude_failures", 0) + 1
    state["last_failure_at"] = now_iso
    save_health_state(HEALTH_STATE_FILE, state)

    count = state["consecutive_claude_failures"]
    print(f"Recorded Claude-analysis failure. Consecutive failures: {count}")

    if count < ALERT_THRESHOLD:
        return 0

    body_text = (
        f"Claude分析ステップが **{count}回連続** で失敗しました。\n\n"
        f"- 最新失敗時刻(UTC): {now_iso}\n"
        f"- 実行URL: {RUN_URL}\n\n"
        "ANTHROPIC_API_KEYの有効期限・残高、Anthropic APIの障害状況、"
        "またはGitHub compare APIの応答形式変更などを確認してください。\n\n"
        "この間、upstream-state.jsonは更新されていません(正常な安全振る舞いです)。"
    )

    existing = find_open_alert_issue(REPO, GH_TOKEN)
    if existing is None:
        gh_api(
            "POST",
            f"/repos/{REPO}/issues",
            GH_TOKEN,
            {"title": ALERT_TITLE, "body": body_text},
        )
        print(f"Created alert issue (threshold {ALERT_THRESHOLD} reached).")
    else:
        gh_api(
            "POST",
            f"/repos/{REPO}/issues/{existing}/comments",
            GH_TOKEN,
            {"body": body_text},
        )
        print(f"Commented on existing alert issue #{existing}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
