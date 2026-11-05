"""Applying a reviewer's decisions.

Accepted candidates change adjusted EBITDA. Rejected and undecided ones do not.
A decision for a candidate that no longer exists is reported, never ignored.
"""

from __future__ import annotations

from .models import Candidate, Decision, ReviewedCandidate


def apply_decisions(
    candidates: list[Candidate], decisions: dict[str, Decision]
) -> tuple[list[ReviewedCandidate], list[str]]:
    reviewed = []
    for c in candidates:
        d = decisions.get(c.id)
        if d is None:
            reviewed.append(ReviewedCandidate(c, "pending", 0, ""))
        elif not d.accepted:
            reviewed.append(ReviewedCandidate(c, "rejected", 0, d.note))
        else:
            effect = c.proposed_effect if d.amount_override is None else d.amount_override
            reviewed.append(ReviewedCandidate(c, "accepted", effect, d.note))

    known = {c.id for c in candidates}
    stale = sorted(f"{d.candidate_id} ({d.source})" for d in decisions.values() if d.candidate_id not in known)
    return reviewed, stale


def adjustments_by_month(reviewed: list[ReviewedCandidate], months: list[str]) -> list[int]:
    index = {m: i for i, m in enumerate(months)}
    totals = [0] * len(months)
    for r in reviewed:
        if r.status == "accepted":
            totals[index[r.candidate.month]] += r.final_effect
    return totals
