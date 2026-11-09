"""Track replies from companies via IMAP, classify them, update applications.

Two-stage classifier: cheap rules first (most ATS emails are templated), the
LLM only for emails the rules can't settle. Matching an email to an
application uses sender domain, company name and job title.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .db import Store
from .llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

Category = Literal["not_job_related", "application_received", "assessment", "interview",
                   "offer", "rejection", "other_job_update"]
STATUS_FOR = {
    "application_received": "in_review",
    "assessment": "assessment",
    "interview": "interview",
    "offer": "offer",
    "rejection": "rejected",
}

# Senders used by ATS platforms on behalf of employers.
ATS_SENDERS = ("greenhouse", "lever.co", "myworkday", "workday", "smartrecruiters", "icims",
               "ashbyhq", "workablemail", "jobvite", "taleo", "successfactors", "bamboohr",
               "hire.lever", "breezy", "recruitee", "naukri", "linkedin.com", "indeed")

RULES: list[tuple[str, re.Pattern]] = [
    ("offer", re.compile(r"\b(offer letter|pleased to (extend|offer)|job offer|offer of employment)\b", re.I)),
    ("rejection", re.compile(
        r"(not (be )?(moving|proceeding) forward|decided to (move|proceed)( forward)? with other|"
        r"(moving|move) forward with (other|another) candidate|"
        r"unfortunately[^.]{0,80}(not|unable)|will not be progressing|position has been filled|"
        r"not selected|regret to inform|pursue other candidates|other candidates whose)", re.I)),
    ("interview", re.compile(
        r"(schedule (an? |your )?(interview|call|chat)|interview (invitation|invite|request)|"
        r"invite you to (an? )?(interview|call)|phone screen|next round|availability for (an? )?"
        r"(interview|call)|calendly\.com|meet the team)", re.I)),
    ("assessment", re.compile(
        r"(online assessment|coding (challenge|test|assessment)|hackerrank|codility|codesignal|"
        r"take[- ]home (assignment|task|exercise)|testgorilla)", re.I)),
    ("application_received", re.compile(
        r"(thank(s| you) for (applying|your application|your interest)|application (has been )?received|"
        r"we('ve| have) received your application|application submitted|successfully applied)", re.I)),
]


class EmailVerdict(BaseModel):
    category: Category
    company: str = Field(description="employer name, empty if unknown")
    job_title: str = Field(description="role mentioned, empty if none")
    summary: str = Field(description="one line: what the email says and any action needed")
    action_needed: bool = Field(description="true if the candidate must reply/schedule/complete something")
    interview_datetime: str = Field(description="ISO date/time of an interview if stated, else empty")


CLASSIFY_SYSTEM = """You triage a job seeker's inbox. Classify one email about job applications.
'interview' includes recruiter screens and scheduling requests. 'assessment' is any test or
take-home. Newsletters, job alerts and recommendations are 'not_job_related'."""


def rule_classify(subject: str, body: str) -> str | None:
    text = f"{subject}\n{body[:4000]}"
    for cat, pat in RULES:  # ordered most → least decisive
        if pat.search(text):
            return cat
    return None


def looks_job_related(sender: str, subject: str, body: str) -> bool:
    s = sender.lower()
    if any(a in s for a in ATS_SENDERS):
        return True
    return bool(re.search(r"\b(application|applied|interview|recruit|position|role|candidate|hiring|offer)\b",
                          f"{subject} {body[:1500]}", re.I))


def _domain_root(addr: str) -> str:
    dom = addr.rsplit("@", 1)[-1].lower()
    parts = dom.split(".")
    return parts[-2] if len(parts) >= 2 else dom


def _norm_company(name: str) -> str:
    n = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|pvt|private|technologies|technology|labs|"
               r"software|solutions|systems|group|india)\b\.?", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


def match_job(store: Store, sender: str, subject: str, body: str, company_hint: str = "",
              title_hint: str = "") -> int | None:
    """Best-matching applied job for an email, or None."""
    candidates = store.jobs(["applied", "in_review", "assessment", "interview", "offer", "tailored"],
                            order="applied_at DESC")
    if not candidates:
        return None
    dom = _domain_root(parseaddr(sender)[1])
    text = re.sub(r"[^a-z0-9 ]", " ", f"{subject} {body[:3000]} {parseaddr(sender)[0]}".lower())
    text_compact = text.replace(" ", "")
    hint = _norm_company(company_hint)
    best, best_score = None, 0
    for j in candidates:
        c = _norm_company(j["company"])
        if not c:
            continue
        score = 0
        if hint and (hint == c or (len(c) > 3 and c in hint) or (len(hint) > 3 and hint in c)):
            score += 5
        if len(c) > 2 and (c == dom or dom.startswith(c) or c.startswith(dom) and len(dom) > 3):
            score += 4
        if len(c) > 3 and c in text_compact:
            score += 3
        title_words = [w for w in re.findall(r"[a-z]{4,}", (j["title"] or "").lower())]
        if title_words and sum(w in text for w in title_words) >= max(1, len(title_words) // 2):
            score += 2
        if title_hint and title_hint.lower() in (j["title"] or "").lower():
            score += 1
        if score > best_score:
            best, best_score = j["id"], score
    return best if best_score >= 3 else None


def _body(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def process_email(store: Store, llm: LLM | None, *, message_id: str, sender: str, subject: str,
                  body: str, date: str) -> dict[str, Any] | None:
    """Classify one email and apply it. Returns what happened, or None if skipped."""
    if store.seen_message(message_id) or not looks_job_related(sender, subject, body):
        return None
    cat = rule_classify(subject, body)
    verdict: EmailVerdict | None = None
    if llm is not None:  # LLM gives company/role/summary/interview time even when rules hit
        try:
            verdict = llm.structured(
                CLASSIFY_SYSTEM, f"From: {sender}\nSubject: {subject}\nDate: {date}\n\n{body[:6000]}",
                EmailVerdict, max_tokens=2000)
            # Trust the rule on decisive templated phrases, the LLM otherwise.
            cat = cat if cat in ("rejection", "offer") else verdict.category
        except LLMUnavailable as e:
            log.warning("email classification fell back to rules: %s", e)
    if cat in (None, "not_job_related"):
        return None
    job_id = match_job(store, sender, subject, body,
                       verdict.company if verdict else "", verdict.job_title if verdict else "")
    new_status = STATUS_FOR.get(cat)
    summary = verdict.summary if verdict else subject
    store.add_event(job_id, "email", status=new_status, message_id=message_id, sender=sender,
                    subject=subject, summary=summary, occurred_at=date,
                    data=verdict.model_dump() if verdict else {"category": cat})
    moved = False
    if job_id and new_status:
        moved = store.set_status(job_id, new_status, source="email")
    return {"category": cat, "job_id": job_id, "status_changed": moved, "subject": subject}


def sync_imap(store: Store, cfg, llm: LLM | None) -> dict[str, int]:
    ec = cfg["email"]
    password = os.environ.get(ec["password_env"])
    if not (ec.get("imap_user") and password):
        raise SystemExit(f"Set email.imap_user in config and the {ec['password_env']} env var "
                         "(for Gmail, create an App Password: Google Account → Security → App passwords).")
    since = (datetime.now(timezone.utc) - timedelta(days=ec["lookback_days"])).strftime("%d-%b-%Y")
    stats = {"fetched": 0, "job_related": 0, "matched": 0, "status_changes": 0}
    with imaplib.IMAP4_SSL(ec["imap_host"]) as M:
        M.login(ec["imap_user"], password)
        M.select(ec["folder"], readonly=True)  # never marks mail as read
        typ, data = M.search(None, f'(SINCE "{since}")')
        ids = data[0].split() if typ == "OK" else []
        for num in ids[-500:]:
            typ, msg_data = M.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
            stats["fetched"] += 1
            mid = (msg.get("Message-ID") or f"imap-{num.decode()}").strip()
            try:
                date = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat()
            except Exception:
                date = datetime.now(timezone.utc).isoformat()
            res = process_email(store, llm, message_id=mid, sender=str(msg.get("From", "")),
                                subject=str(msg.get("Subject", "")), body=_body(msg), date=date)
            if res:
                stats["job_related"] += 1
                stats["matched"] += int(bool(res["job_id"]))
                stats["status_changes"] += int(res["status_changed"])
    return stats
