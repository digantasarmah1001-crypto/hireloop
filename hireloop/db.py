"""SQLite storage. Every row carries user_id so this can go multi-user later."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Application lifecycle. Order matters: a status only moves "forward" unless
# the user overrides it (see Store.set_status(force=True)).
STATUSES = [
    "discovered",    # found by search
    "shortlisted",   # passed fit scoring
    "skipped",       # user or filters said no
    "tailored",      # resume + cover letter generated
    "applied",       # submitted
    "in_review",     # confirmation / "we're reviewing" email
    "assessment",    # online test / take-home
    "interview",     # interview scheduled or happened
    "offer",
    "accepted",      # final: got the job and took it
    "declined",      # final: got an offer, said no
    "rejected",      # final
    "ghosted",       # final: no response after follow-ups
    "withdrawn",     # final
]
FINAL = {"accepted", "declined", "rejected", "ghosted", "withdrawn"}
RANK = {s: i for i, s in enumerate(STATUSES)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT, email TEXT UNIQUE,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL DEFAULT 1,
  fingerprint TEXT NOT NULL,          -- dedupe across boards
  site TEXT, job_url TEXT, apply_url TEXT,
  title TEXT, company TEXT, location TEXT,
  description TEXT, is_remote INTEGER,
  salary TEXT, date_posted TEXT,
  ats TEXT,                            -- greenhouse / lever / workday / ...
  fit_score REAL, fit_reasons TEXT,    -- JSON
  keyword_score REAL, missing_keywords TEXT,  -- JSON
  status TEXT NOT NULL DEFAULT 'discovered',
  resume_path TEXT, cover_letter_path TEXT,
  applied_at TEXT, last_activity_at TEXT,
  outcome_note TEXT,
  created_at TEXT, updated_at TEXT,
  UNIQUE(user_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL DEFAULT 1,
  job_id INTEGER,                       -- NULL if email couldn't be matched
  kind TEXT NOT NULL,                   -- status_change / email / note / checkin
  status TEXT,                          -- status implied by this event
  message_id TEXT,                      -- email Message-ID (dedupe)
  sender TEXT, subject TEXT, summary TEXT,
  occurred_at TEXT, created_at TEXT,
  data TEXT,                            -- JSON
  UNIQUE(user_id, message_id)
);
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL DEFAULT 1,
  job_id INTEGER NOT NULL,
  question TEXT NOT NULL,
  reason TEXT,
  answer TEXT,
  created_at TEXT, answered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(user_id, status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(company: str, title: str, location: str = "") -> str:
    norm = lambda s: " ".join((s or "").lower().split())  # noqa: E731
    return hashlib.sha1(f"{norm(company)}|{norm(title)}|{norm(location)}".encode()).hexdigest()[:16]


class Store:
    def __init__(self, path: str | Path, user_id: int = 1):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.user_id = user_id
        self.conn.execute(
            "INSERT OR IGNORE INTO users(id, created_at) VALUES (?, ?)", (user_id, now())
        )
        self.conn.commit()

    # ---------- jobs ----------
    def upsert_job(self, job: dict[str, Any]) -> tuple[int, bool]:
        """Insert a job if new. Returns (job_id, created)."""
        fp = job.get("fingerprint") or fingerprint(job["company"], job["title"], job.get("location", ""))
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE user_id=? AND fingerprint=?", (self.user_id, fp)
        ).fetchone()
        if row:
            return row["id"], False
        cols = [
            "site", "job_url", "apply_url", "title", "company", "location",
            "description", "is_remote", "salary", "date_posted", "ats",
        ]
        vals = [job.get(c) for c in cols]
        ts = now()
        cur = self.conn.execute(
            f"INSERT INTO jobs(user_id, fingerprint, {', '.join(cols)}, created_at, updated_at) "
            f"VALUES (?, ?, {', '.join('?' * len(cols))}, ?, ?)",
            (self.user_id, fp, *vals, ts, ts),
        )
        self.conn.commit()
        return cur.lastrowid, True

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        for k, v in list(fields.items()):
            if isinstance(v, (list, dict)):
                fields[k] = json.dumps(v)
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE jobs SET {sets} WHERE id=? AND user_id=?", (*fields.values(), job_id, self.user_id)
        )
        self.conn.commit()

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, self.user_id)
        ).fetchone()

    def jobs(self, statuses: Iterable[str] | None = None, order: str = "fit_score DESC") -> list[sqlite3.Row]:
        q = "SELECT * FROM jobs WHERE user_id=?"
        args: list[Any] = [self.user_id]
        if statuses:
            statuses = list(statuses)
            q += f" AND status IN ({', '.join('?' * len(statuses))})"
            args += statuses
        allowed = {"fit_score DESC", "updated_at DESC", "applied_at DESC", "created_at DESC"}
        q += f" ORDER BY {order if order in allowed else 'fit_score DESC'}"
        return self.conn.execute(q, args).fetchall()

    def set_status(self, job_id: int, status: str, *, force: bool = False, note: str = "",
                   source: str = "user") -> bool:
        """Move a job to a status. Without force, only forward moves are applied,
        so an old 'application received' email can't undo an 'interview'."""
        if status not in RANK:
            raise ValueError(f"unknown status {status!r}")
        job = self.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        cur = job["status"]
        if not force:
            if cur in FINAL or cur == status:
                return False
            if status not in FINAL and RANK[status] <= RANK[cur]:
                return False
        fields: dict[str, Any] = {"status": status, "last_activity_at": now()}
        if status == "applied" and not job["applied_at"]:
            fields["applied_at"] = now()
        if note:
            fields["outcome_note"] = note
        self.update_job(job_id, **fields)
        self.add_event(job_id, "status_change", status=status, summary=f"{cur} → {status} ({source})")
        return True

    # ---------- events ----------
    def add_event(self, job_id: int | None, kind: str, *, status: str | None = None,
                  message_id: str | None = None, sender: str = "", subject: str = "",
                  summary: str = "", occurred_at: str | None = None, data: dict | None = None) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO events(user_id, job_id, kind, status, message_id, sender, subject, summary,"
                " occurred_at, created_at, data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.user_id, job_id, kind, status, message_id, sender, subject, summary,
                 occurred_at or now(), now(), json.dumps(data or {})),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:  # duplicate email
            return False

    def seen_message(self, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM events WHERE user_id=? AND message_id=?", (self.user_id, message_id)
        ).fetchone() is not None

    def events(self, job_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if job_id is None:
            return self.conn.execute(
                "SELECT * FROM events WHERE user_id=? ORDER BY occurred_at DESC LIMIT ?",
                (self.user_id, limit)).fetchall()
        return self.conn.execute(
            "SELECT * FROM events WHERE user_id=? AND job_id=? ORDER BY occurred_at DESC",
            (self.user_id, job_id)).fetchall()

    # ---------- check-ins ----------
    def open_checkin(self, job_id: int, question: str, reason: str) -> int | None:
        exists = self.conn.execute(
            "SELECT id FROM checkins WHERE user_id=? AND job_id=? AND reason=? AND answer IS NULL",
            (self.user_id, job_id, reason)).fetchone()
        if exists:
            return None
        cur = self.conn.execute(
            "INSERT INTO checkins(user_id, job_id, question, reason, created_at) VALUES (?,?,?,?,?)",
            (self.user_id, job_id, question, reason, now()))
        self.conn.commit()
        return cur.lastrowid

    def pending_checkins(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, j.title, j.company, j.status FROM checkins c JOIN jobs j ON j.id=c.job_id "
            "WHERE c.user_id=? AND c.answer IS NULL ORDER BY c.created_at", (self.user_id,)).fetchall()

    def answer_checkin(self, checkin_id: int, answer: str) -> sqlite3.Row:
        self.conn.execute("UPDATE checkins SET answer=?, answered_at=? WHERE id=? AND user_id=?",
                          (answer, now(), checkin_id, self.user_id))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM checkins WHERE id=?", (checkin_id,)).fetchone()

    def checkin_history(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM checkins WHERE user_id=? AND job_id=? ORDER BY created_at",
            (self.user_id, job_id)).fetchall()

    # ---------- stats ----------
    def funnel(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM jobs WHERE user_id=? GROUP BY status", (self.user_id,)
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        return {s: counts.get(s, 0) for s in STATUSES}

    def applied_today(self) -> int:
        today = now()[:10]
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id=? AND substr(applied_at,1,10)=?",
            (self.user_id, today)).fetchone()[0]
