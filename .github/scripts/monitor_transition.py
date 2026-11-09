"""Fail-closed, side-effect-free upstream monitor transition checks.

Staging component only: no GitHub calls, no notification, no workflow wiring.
"""


def may_advance_seen_sha(
    *,
    fetched_sha: str,
    analyzed_sha: str | None,
    notification_required: bool,
    notification_confirmed: bool,
    bookkeeping_persisted: bool,
    state_persistable: bool,
) -> bool:
    """Do not mark an update seen if analysis or required delivery is uncertain."""
    if not isinstance(fetched_sha, str) or len(fetched_sha) != 40:
        return False
    if any(c not in "0123456789abcdef" for c in fetched_sha):
        return False
    return (
        analyzed_sha == fetched_sha
        and (not notification_required or notification_confirmed)
        and bookkeeping_persisted
        and state_persistable
    )
