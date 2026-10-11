#!/usr/bin/env python3
"""Apply one health-state mutation to whatever is CURRENTLY on disk.

Used two ways by the calling workflow step:
1. Once, right after analyze_upstream_diff.py runs (normal path; usually a
   no-op duplicate of what that script already wrote, kept idempotent on
   purpose so this script is also step 2 below).
2. Again inside the git-push retry loop, after `git fetch` + `git reset
   --hard origin/<branch>`, to RE-DERIVE the correct new value relative to
   whatever is actually on the remote now -- never by replaying a stale
   git commit (which, for a small mutable counter file, can conflict on
   rebase even when the two writers' *intent* doesn't actually conflict;
   proven by direct test against a simulated race). This script never
   calls Claude or any network API, so re-running it costs nothing and has
   no side effects beyond the local file -- safe to retry any number of
   times.

Usage: bump_health_state.py <mutation>
  mutation is 'none', 'success', or 'budget_alert:<YYYY-MM>', matching the
  `health_mutation` output of analyze_upstream_diff.py.
"""
from __future__ import annotations

import datetime
import json
import os
import sys

HEALTH_STATE_FILE = os.environ.get("HEALTH_STATE_FILE", ".github/monitor-health-state.json")


def current_month() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def load_health_state(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "consecutive_claude_failures": 0,
            "calls_this_month": 0,
            "budget_month": "",
            "last_success_at": None,
            "last_failure_at": None,
            "budget_alert_sent_for_month": None,
        }
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("budget_alert_sent_for_month", None)
    return state


def save_health_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bump_health_state.py <none|success|budget_alert:MONTH>", file=sys.stderr)
        return 1
    mutation = sys.argv[1]

    if mutation in ("", "none"):
        return 0

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    month = current_month()
    state = load_health_state(HEALTH_STATE_FILE)
    if state.get("budget_month") != month:
        state["budget_month"] = month
        state["calls_this_month"] = 0

    if mutation == "success":
        state["consecutive_claude_failures"] = 0
        state["calls_this_month"] = state.get("calls_this_month", 0) + 1
        state["last_success_at"] = now_iso
    elif mutation.startswith("budget_alert:"):
        alert_month = mutation.split(":", 1)[1]
        state["budget_alert_sent_for_month"] = alert_month
    else:
        print(f"Unknown mutation: {mutation!r}", file=sys.stderr)
        return 1

    save_health_state(HEALTH_STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
