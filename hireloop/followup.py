"""The outcome loop: ask the user what happened, and record the answer.

Email only tells part of the story (phone calls, LinkedIn messages, verbal
offers). So hireloop opens "check-ins" — questions shown in the CLI and
dashboard — at the moments where only the user knows the answer.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .db import FINAL, Store

QUESTIONS = {
    "offer": "You got an offer from {company} for {title}. Did you accept it?",
    "interview_done": "How did the {title} interview at {company} go? Did you get the job?",
    "no_reply": "No reply from {company} for {title} in {days} days. Heard anything outside email?",
}

# Answer choices per check-in reason → resulting status (None = no change).
ANSWERS = {
    "offer": {"accepted": "accepted", "declined": "declined", "still deciding": None},
    "interview_done": {"got the job": "offer", "rejected": "rejected", "next round": "interview",
                       "still waiting": None},
    "no_reply": {"interviewing": "interview", "rejected": "rejected", "no news": None,
                 "give up (ghosted)": "ghosted", "withdraw": "withdrawn"},
}


def _days_since(ts: str | None) -> float:
    if not ts:
        return 0
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def generate_checkins(store: Store, cfg) -> int:
    fc = cfg["followup"]
    created = 0
    for j in store.jobs(["applied", "in_review", "assessment", "interview", "offer"]):
        if j["status"] in FINAL:
            continue
        last = j["last_activity_at"] or j["applied_at"]
        reason = None
        if j["status"] == "offer":
            reason = "offer"
        elif j["status"] == "interview" and _days_since(last) >= fc["ask_after_interview_days"]:
            reason = "interview_done"
        elif j["status"] in ("applied", "in_review") and _days_since(last) >= fc["ask_after_days_no_reply"]:
            reason = "no_reply"
        if reason:
            q = QUESTIONS[reason].format(company=j["company"], title=j["title"], days=int(_days_since(last)))
            if store.open_checkin(j["id"], q, reason):
                created += 1
    return created


def apply_answer(store: Store, checkin_id: int, answer: str) -> str | None:
    """Record an answer. Returns the new status, if any."""
    row = store.answer_checkin(checkin_id, answer)
    target = ANSWERS.get(row["reason"], {}).get(answer)
    if target:
        store.set_status(row["job_id"], target, force=True, source="you")
    else:
        # touch activity so we don't re-ask immediately
        store.update_job(row["job_id"], last_activity_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.add_event(row["job_id"], "checkin", status=target, summary=f"{row['question']} → {answer}")
    return target
