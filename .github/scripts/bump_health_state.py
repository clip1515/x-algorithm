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
   proven by direct test against a simulated race). This script makes no
   network call of any kind, so re-running it costs nothing and has no
   side effects beyond the local file -- safe to retry any number of
   times.

Usage: bump_health_state.py <mutation>
  mutation is 'none' or 'success', matching the `health_mutation` output
  of analyze_upstream_diff.py.
"""
from __future__ import annotations

import datetime
import json
import os
import sys

HEALTH_STATE_FILE = os.environ.get("HEALTH_STATE_FILE", ".github/monitor-health-state.json")


def load_health_state(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "consecutive_analysis_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
        }
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    if "consecutive_analysis_failures" not in state and "consecutive_claude_failures" in state:
        state["consecutive_analysis_failures"] = state.pop("consecutive_claude_failures")
    state.setdefault("consecutive_analysis_failures", 0)
    state.setdefault("last_success_at", None)
    state.setdefault("last_failure_at", None)
    for obsolete_key in ("calls_this_month", "budget_month", "budget_alert_sent_for_month", "consecutive_claude_failures"):
        state.pop(obsolete_key, None)
    return state


def save_health_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bump_health_state.py <none|success>", file=sys.stderr)
        return 1
    mutation = sys.argv[1]

    if mutation in ("", "none"):
        return 0

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = load_health_state(HEALTH_STATE_FILE)

    if mutation == "success":
        state["consecutive_analysis_failures"] = 0
        state["last_success_at"] = now_iso
    else:
        print(f"Unknown mutation: {mutation!r}", file=sys.stderr)
        return 1

    save_health_state(HEALTH_STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
