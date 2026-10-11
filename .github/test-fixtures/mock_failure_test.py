"""Test-only harness: run record_monitor_failure.main() with GitHub API
calls stubbed out, so the consecutive-failure counter / alert-threshold
logic can be exercised in CI without making real GitHub API calls.
Never shipped to main; lives only on the test verification branch.
"""
import json
import sys

sys.path.insert(0, ".github/scripts")
import record_monitor_failure as m  # noqa: E402

calls = []


def fake_gh_api(method, path, token, body=None):
    calls.append({"method": method, "path": path, "body": body})
    if method == "GET" and "/search/issues" in path:
        return {"items": []}
    return {"number": 999}


m.gh_api = fake_gh_api

rc = m.main()
print("record_monitor_failure.main() returned:", rc)
for c in calls:
    print("MOCK GitHub API call:", json.dumps(c, ensure_ascii=False))
