#!/usr/bin/env python3
"""Analyze an upstream commit range with a free, local, rule-based
(keyword/path regex) classifier and decide whether it warrants a
notification Issue.

No paid API of any kind is called here. The classifier is a plain-Python
regex scan over changed filenames and patch text; it has no semantic
understanding of the diff, so its narrative fields are deliberately
conservative and templated rather than free-form generated text.

Reads configuration from environment variables (set by the calling GitHub
Actions step) and writes results to $GITHUB_OUTPUT plus two local files:
  - the health-state file (consecutive-failure count, last success/failure
    time -- failures here mean "the GitHub compare API call failed", not
    anything related to a paid model, since there is none)
  - the audit log (one JSON line per decision, including important=false)

Exits non-zero on any failure (GitHub compare API error, malformed
response). The caller step has no `set -e`-defeating fallback: a non-zero
exit here must fail the Action step so that later steps (Issue creation,
state advancement) are skipped by GitHub Actions' default implicit
success() gating.

This script does not perform any git operations itself; the calling
workflow is responsible for staging/committing the files this script
writes.
"""
from __future__ import annotations

import datetime
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars for testing / tuning)
# ---------------------------------------------------------------------------

UPSTREAM_REPO = os.environ["UPSTREAM_REPO"]
PREV_SHA = os.environ["PREV_SHA"]
NEW_SHA = os.environ["NEW_SHA"]
GH_TOKEN = os.environ["GH_TOKEN"]

HEALTH_STATE_FILE = os.environ.get("HEALTH_STATE_FILE", ".github/monitor-health-state.json")
AUDIT_LOG_FILE = os.environ.get("AUDIT_LOG_FILE", ".github/notify-audit-log.jsonl")

# Defensive bound on how much of one file's patch text we regex-scan. Not a
# cost control (there is no paid call to bound) -- just a sanity cap against
# a pathologically huge single-file diff wasting CI time.
MAX_PATCH_SCAN_CHARS = int(os.environ.get("MAX_PATCH_SCAN_CHARS", "200000"))

# Test-only hooks: when set, these short-circuit the real network calls so
# the whole pipeline can be exercised in CI without touching the real
# GitHub Issues API side effects that depend on this script's output.
MOCK_COMPARE_RESPONSE_FILE = os.environ.get("MOCK_COMPARE_RESPONSE_FILE", "")
MOCK_SKIP_GH_ISSUE_CALLS = os.environ.get("MOCK_SKIP_GH_ISSUE_CALLS", "") == "true"

GITHUB_OUTPUT = os.environ.get("GITHUB_OUTPUT")

# Path patterns that, if they cover every changed file, mean "not important"
# without ever running the classifier (requirement: README/CI/tests-only
# changes are never treated as important).
TRIVIAL_PATH_PATTERNS = [
    "README*",
    "readme*",
    "docs/*",
    "docs/**",
    "*.md",
    "CHANGELOG*",
    "LICENSE*",
    ".github/workflows/*",
    ".github/ISSUE_TEMPLATE/*",
    "tests/*",
    "tests/**",
    "test/*",
    "test/**",
    "*_test.*",
    "*.test.*",
]

ALLOWED_CATEGORIES = {
    "engagement_weight",
    "dwell_time",
    "cold_start",
    "phoenix",
    "visibility_filtering",
    "safety",
    "reach_ranking_impact",
    "other",
}
ALLOWED_IMPLEMENTED_STATUS = {"active_default", "experimental_flagged", "unclear"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "important": {"type": "boolean"},
        "categories": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(ALLOWED_CATEGORIES)},
        },
        "implemented_status": {
            "type": "string",
            "enum": sorted(ALLOWED_IMPLEMENTED_STATUS),
        },
        "summary_ja": {"type": "string"},
        "before_after_ja": {"type": "string"},
        "impact_on_posting_ja": {"type": "string"},
        "recommended_action_ja": {"type": "string"},
        "confirmed_facts_ja": {"type": "array", "items": {"type": "string"}},
        "inference_ja": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": sorted(ALLOWED_CONFIDENCE)},
    },
    "required": [
        "important",
        "categories",
        "implemented_status",
        "summary_ja",
        "before_after_ja",
        "impact_on_posting_ja",
        "recommended_action_ja",
        "confirmed_facts_ja",
        "inference_ja",
        "confidence",
    ],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Rule-based classifier: named categories first (regex over filename + patch
# text), then a lower-confidence generic ranking/reach catch-all for diffs
# that are plausibly relevant but don't match a specific named category.
#
# Boundary note: real code identifiers are snake_case/kebab-case/camelCase
# (e.g. `phoenix_scorer`, `safety_score`, `cold_start_v2`), and plain regex
# `\b` fails to find a boundary between a word and an adjacent `_` (both
# count as "word characters"). `_LB`/`_RB` below are boundary assertions
# that treat any non-letter (underscore, hyphen, digit, slash, whitespace,
# punctuation) as a valid edge, while still rejecting a match that's really
# the middle of a different identifier (e.g. "phoenixfoo" or "xphoenix"):
#   _LB  no letter immediately before the match
#   _RB  no LOWERCASE letter immediately after (an uppercase letter is
#        allowed through, so PascalCase/camelCase like `PhoenixScorer`
#        still matches `phoenix` at its start)
# ---------------------------------------------------------------------------

_LB = r"(?<![A-Za-z])"
# (?-i:[a-z]) scopes case-SENSITIVITY back on for just this character class,
# overriding the pattern's own re.IGNORECASE -- otherwise IGNORECASE would
# make [a-z] match uppercase too, and this boundary would reject the valid
# PascalCase/camelCase continuation (e.g. "PhoenixScorer") it's meant to allow.
_RB = r"(?!(?-i:[a-z]))"


def _kw(pattern: str) -> re.Pattern[str]:
    """Wrap a raw keyword/alternation fragment with the boundary assertions
    above and compile case-insensitively."""
    return re.compile(_LB + pattern + _RB, re.IGNORECASE)


CATEGORY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "phoenix": [_kw(r"phoenix")],
    "cold_start": [_kw(r"cold[_\-\s]?start")],
    "engagement_weight": [
        _kw(r"engagement[_\-\s]?weight"),
        _kw(r"(like|reply|repost|retweet|quote|favorite|fav)[_\-]?weight"),
    ],
    "dwell_time": [_kw(r"dwell[_\-\s]?time")],
    "visibility_filtering": [
        _kw(r"visibility[_\-\s]?filter(ing)?"),
        # "vf" is the real upstream codebase's own standard abbreviation
        # for visibility filtering (e.g. `vf_candidate_hydrator.rs`,
        # `VfClient`, `XaiVfClient`, `vf_pb`, `XAI_VF_MAX_BATCH_SIZE`) --
        # confirmed by scanning the actual xai-org/x-algorithm source
        # (928 matches, zero false positives found) during pre-merge
        # verification. Without this, files outside the
        # visibility-filtering/ directory that still touch VF logic
        # (like the real commit "Deduplicate in_network_ids before VF
        # lookup") would be silently missed.
        _kw(r"vf"),
    ],
    "safety": [
        _kw(r"safety"),
        _kw(r"toxicit(y|ies)"),
        _kw(r"harassment"),
        _kw(r"abuse"),
        _kw(r"nsfw"),
    ],
}

# Matches here alone (no specific category hit) still flag important=True,
# but only at "low" confidence and category "other" -- broad terms that
# commonly appear across an algorithm repo, so they're a catch-all signal
# rather than a precise one.
GENERIC_RANKING_PATTERNS: list[re.Pattern[str]] = [
    _kw(r"ranking"),
    _kw(r"scorer"),
    _kw(r"scoring"),
    _kw(r"reach"),
    _kw(r"recommend\w*"),
]

# Signals that a matched change is gated behind a flag/experiment rather
# than active for everyone. Absence of this pattern does NOT mean the
# change is active by default -- see rule_based_classify()'s
# implemented_status logic: we only ever claim "experimental_flagged" or
# "unclear", never "active_default", since a regex scan cannot actually
# confirm a change is on by default (requirement: when it can't be
# determined for certain, report 'unclear').
EXPERIMENTAL_HINT_PATTERN = _kw(
    r"(experiment(al)?\w*|treatment|feature[_\-]?flag\w*|canary|rollout|a/?b[_\-]?test\w*|gated?)"
)


class AnalysisError(Exception):
    """Raised for any condition that should fail this step (and thus the job)."""


def http_get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AnalysisError(f"GitHub compare API HTTP error {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise AnalysisError(f"GitHub compare API network error: {e}") from e


def fetch_compare(upstream_repo: str, prev_sha: str, new_sha: str, token: str) -> dict:
    """Fetch the compare between prev_sha and new_sha, handling GitHub's
    known truncation points:
      - `commits` is capped (GitHub returns at most 250); `total_commits`
        tells the real count.
      - `files` is capped at 300; a count of exactly 300 is a truncation
        signal, not necessarily the true total.
      - Individual file `patch` text is omitted by GitHub for very large or
        binary files.
    Returns a normalized dict: {commits_truncated, total_commits,
    files_truncated, files: [{filename, status, patch_or_none}], compare_url}
    """
    if MOCK_COMPARE_RESPONSE_FILE:
        with open(MOCK_COMPARE_RESPONSE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        url = (
            f"https://api.github.com/repos/{upstream_repo}/compare/"
            f"{prev_sha}...{new_sha}?per_page=100"
        )
        data = http_get_json(url, token)

    commits = data.get("commits", [])
    total_commits = data.get("total_commits", len(commits))
    commits_truncated = total_commits > len(commits)

    files = data.get("files", [])
    # GitHub's documented cap on the files array for one compare response.
    files_truncated = len(files) >= 300

    normalized_files = [
        {
            "filename": f.get("filename", ""),
            "status": f.get("status", ""),
            "patch": f.get("patch"),  # may be absent/None for large/binary files
        }
        for f in files
    ]

    return {
        "commits_truncated": commits_truncated,
        "total_commits": total_commits,
        "fetched_commits": len(commits),
        "files_truncated": files_truncated,
        "files": normalized_files,
        "compare_url": data.get("html_url", ""),
    }


def is_trivial_only(files: list[dict]) -> bool:
    if not files:
        # No file-level detail at all (shouldn't normally happen for a real
        # diff) - treat conservatively as non-trivial so a human can still
        # look at it rather than silently dropping it.
        return False
    for f in files:
        name = f["filename"]
        if not any(fnmatch.fnmatch(name, pat) for pat in TRIVIAL_PATH_PATTERNS):
            return False
    return True


def validate_schema(obj: dict) -> None:
    """Lightweight structural validation of the classifier's own output.
    The classifier always builds dicts matching this shape, so a failure
    here would indicate a bug in rule_based_classify() itself -- this is a
    defensive self-check, not a defense against an external API."""
    if not isinstance(obj, dict):
        raise AnalysisError("analysis result is not a JSON object")

    missing = [k for k in RESULT_SCHEMA["required"] if k not in obj]
    if missing:
        raise AnalysisError(f"analysis result missing required fields: {missing}")

    extra = set(obj.keys()) - set(RESULT_SCHEMA["properties"].keys())
    if extra:
        raise AnalysisError(f"analysis result has unexpected fields: {sorted(extra)}")

    if not isinstance(obj["important"], bool):
        raise AnalysisError("'important' must be a boolean")

    if not isinstance(obj["categories"], list) or not all(
        isinstance(c, str) for c in obj["categories"]
    ):
        raise AnalysisError("'categories' must be a list of strings")
    bad_categories = [c for c in obj["categories"] if c not in ALLOWED_CATEGORIES]
    if bad_categories:
        raise AnalysisError(f"'categories' has unknown values: {bad_categories}")

    if obj["implemented_status"] not in ALLOWED_IMPLEMENTED_STATUS:
        raise AnalysisError(f"'implemented_status' has unexpected value: {obj['implemented_status']!r}")

    if obj["confidence"] not in ALLOWED_CONFIDENCE:
        raise AnalysisError(f"'confidence' has unexpected value: {obj['confidence']!r}")

    for key in ("summary_ja", "before_after_ja", "impact_on_posting_ja", "recommended_action_ja"):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise AnalysisError(f"'{key}' must be a non-empty string")

    for key in ("confirmed_facts_ja", "inference_ja"):
        if not isinstance(obj[key], list) or not all(isinstance(x, str) for x in obj[key]):
            raise AnalysisError(f"'{key}' must be a list of strings")


def _classify_file(f: dict) -> dict:
    """Scan one file's name + patch text for category hits."""
    name = f["filename"]
    patch = (f.get("patch") or "")[:MAX_PATCH_SCAN_CHARS]

    matched_in_path: set[str] = set()
    matched_in_patch: set[str] = set()
    for category, patterns in CATEGORY_PATTERNS.items():
        for pat in patterns:
            if pat.search(name):
                matched_in_path.add(category)
            if pat.search(patch):
                matched_in_patch.add(category)

    generic_hit = any(pat.search(name) or pat.search(patch) for pat in GENERIC_RANKING_PATTERNS)
    experimental_hint = bool(
        EXPERIMENTAL_HINT_PATTERN.search(name) or EXPERIMENTAL_HINT_PATTERN.search(patch)
    )

    return {
        "filename": name,
        "matched_in_path": matched_in_path,
        "matched_in_patch": matched_in_patch,
        "generic_hit": generic_hit,
        "experimental_hint": experimental_hint,
    }


def rule_based_classify(files: list[dict]) -> dict:
    """Free, local, regex-based replacement for the old Claude call.
    Returns a dict matching RESULT_SCHEMA. No network call, no external
    service of any kind."""
    per_file = [_classify_file(f) for f in files]

    all_categories: set[str] = set()
    path_matched_categories: set[str] = set()
    patch_matched_categories: set[str] = set()
    experimental_hint_any = False
    generic_hit_any = False
    confirmed_facts: list[str] = []

    for pf in per_file:
        cats = pf["matched_in_path"] | pf["matched_in_patch"]
        if cats:
            all_categories |= cats
            path_matched_categories |= pf["matched_in_path"]
            patch_matched_categories |= pf["matched_in_patch"]
            for c in sorted(cats):
                where = []
                if c in pf["matched_in_path"]:
                    where.append("ファイル名")
                if c in pf["matched_in_patch"]:
                    where.append("差分内容")
                confirmed_facts.append(
                    f"`{pf['filename']}` の{'・'.join(where)}に '{c}' 関連のキーワードを検出しました。"
                )
            if pf["experimental_hint"]:
                experimental_hint_any = True
        elif pf["generic_hit"]:
            generic_hit_any = True
            confirmed_facts.append(
                f"`{pf['filename']}` にランキング/リーチ関連の一般的なキーワードを検出しました"
                "(具体的カテゴリは特定できませんでした)。"
            )
            if pf["experimental_hint"]:
                experimental_hint_any = True

    if all_categories:
        categories = sorted(all_categories)
        important = True
        if path_matched_categories and patch_matched_categories:
            confidence = "high"
        elif path_matched_categories or patch_matched_categories:
            confidence = "medium"
        else:
            confidence = "low"
    elif generic_hit_any:
        categories = ["other"]
        important = True
        confidence = "low"
    else:
        categories = []
        important = False
        confidence = "low"

    # Never claim "active_default": a keyword/path scan cannot confirm a
    # change is active by default. Only claim "experimental_flagged" when a
    # gating keyword was actually seen; otherwise "unclear" -- per
    # requirement, uncertainty must be reported as unclear, never guessed.
    implemented_status = "experimental_flagged" if (important and experimental_hint_any) else "unclear"

    if not important:
        summary_ja = "ルールベース検知の結果、対象カテゴリに関連するキーワードは見つかりませんでした。"
        before_after_ja = "重要度なしと判定されたため、詳細分析は行っていません。"
        impact_on_posting_ja = "特になし(検知された関連変更はありません)。"
        recommended_action_ja = "特別な対応は不要です。"
        if not confirmed_facts:
            confirmed_facts = ["対象カテゴリに関連するキーワードは検出されませんでした。"]
    else:
        cat_label = "、".join(categories)
        summary_ja = f"この差分から、以下のカテゴリに関連する変更の可能性を検知しました: {cat_label}。"
        before_after_ja = (
            "ルールベース(キーワード検知)による判定のため、具体的な変更前後の値は分析していません。"
            "下記の差分URLで直接ご確認ください。"
        )
        impact_on_posting_ja = (
            "X投稿運用への具体的な影響の有無は、このツールでは判定できません。"
            "差分内容を人力でご確認ください。"
        )
        recommended_action_ja = "差分URLを開き、該当箇所のコード変更を確認してください。"

    inference_ja = [
        "これはキーワード・パス照合によるルールベース検知であり、実際の挙動変化や"
        "実装意図を保証するものではありません。"
    ]

    return {
        "important": important,
        "categories": categories,
        "implemented_status": implemented_status,
        "summary_ja": summary_ja,
        "before_after_ja": before_after_ja,
        "impact_on_posting_ja": impact_on_posting_ja,
        "recommended_action_ja": recommended_action_ja,
        "confirmed_facts_ja": confirmed_facts,
        "inference_ja": inference_ja,
        "confidence": confidence,
    }


def load_health_state(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "consecutive_analysis_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
        }
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    # Migrate from the old Claude-based schema (PR #4): rename the
    # failure counter and drop the budget-related fields that no longer
    # apply now that there is no paid call to budget.
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


def append_audit_log(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_filter_stage_output(stage: str) -> None:
    """Tells the calling workflow which path this run took: 'path_prefilter'
    or 'rule_based'."""
    if GITHUB_OUTPUT is None:
        return
    with open(GITHUB_OUTPUT, "a", encoding="utf-8") as f:
        f.write(f"filter_stage={stage}\n")


def write_health_mutation_output(mutation: str) -> None:
    """Tells the calling workflow step which health-state mutation (if any)
    needs to be persisted: 'none' or 'success'. The YAML step applies this
    via bump_health_state.py, which is safe to re-run against a freshly
    fetched base after a push retry."""
    if GITHUB_OUTPUT is None:
        return
    with open(GITHUB_OUTPUT, "a", encoding="utf-8") as f:
        f.write(f"health_mutation={mutation}\n")


def write_output(important: bool, fields: dict | None) -> None:
    if GITHUB_OUTPUT is None:
        return
    with open(GITHUB_OUTPUT, "a", encoding="utf-8") as f:
        f.write(f"important={'true' if important else 'false'}\n")
        if fields:
            f.write(f"categories={', '.join(fields['categories'])}\n")
            f.write(f"implemented_status={fields['implemented_status']}\n")
            f.write(f"confidence={fields['confidence']}\n")
            for key in (
                "summary_ja",
                "before_after_ja",
                "impact_on_posting_ja",
                "recommended_action_ja",
            ):
                marker = f"__EOF_{key}__"
                f.write(f"{key}<<{marker}\n{fields[key]}\n{marker}\n")
            for key in ("confirmed_facts_ja", "inference_ja"):
                marker = f"__EOF_{key}__"
                joined = "\n".join(f"- {x}" for x in fields[key])
                f.write(f"{key}<<{marker}\n{joined}\n{marker}\n")


def main() -> int:
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    health = load_health_state(HEALTH_STATE_FILE)

    compare = fetch_compare(UPSTREAM_REPO, PREV_SHA, NEW_SHA, GH_TOKEN)

    audit_base = {
        "timestamp": now_iso,
        "prev_sha": PREV_SHA,
        "new_sha": NEW_SHA,
        "compare_url": compare["compare_url"],
        "total_commits": compare["total_commits"],
        "commits_truncated": compare["commits_truncated"],
        "files_truncated": compare["files_truncated"],
        "file_count": len(compare["files"]),
    }

    if is_trivial_only(compare["files"]):
        audit_base.update(
            {
                "filter_stage": "path_prefilter",
                "important": False,
                "reason": "all changed files matched trivial (docs/CI/test) patterns",
            }
        )
        append_audit_log(AUDIT_LOG_FILE, audit_base)
        write_output(False, None)
        write_health_mutation_output("none")
        write_filter_stage_output("path_prefilter")
        print("Pre-filter: all changed files are trivial (docs/CI/tests). Skipping analysis.")
        return 0

    result = rule_based_classify(compare["files"])
    validate_schema(result)

    health["consecutive_analysis_failures"] = 0
    health["last_success_at"] = now_iso
    save_health_state(HEALTH_STATE_FILE, health)

    audit_base.update(
        {
            "filter_stage": "rule_based",
            "important": result["important"],
            "categories": result["categories"],
            "implemented_status": result["implemented_status"],
            "confidence": result["confidence"],
        }
    )
    append_audit_log(AUDIT_LOG_FILE, audit_base)

    write_output(result["important"], result)
    write_health_mutation_output("success")
    write_filter_stage_output("rule_based")
    print(f"Rule-based analysis complete: important={result['important']} categories={result['categories']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AnalysisError as e:
        print(f"Analysis failed: {e}", file=sys.stderr)
        sys.exit(1)
