"""Job discovery (via JobSpy) and fit scoring."""
from __future__ import annotations

import logging
import math
from typing import Any, Iterable

from pydantic import BaseModel, Field

from .ats import detect_ats, keyword_score
from .db import Store, fingerprint
from .llm import LLM, LLMUnavailable
from .resume import Resume

log = logging.getLogger(__name__)


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        import pandas as pd

        if v is pd.NaT:
            return None
    except ImportError:
        pass
    return v


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """JobSpy DataFrame row -> hireloop job dict."""
    r = {k: _clean(v) for k, v in row.items()}
    apply_url = r.get("job_url_direct") or r.get("job_url")
    salary = None
    if r.get("min_amount") or r.get("max_amount"):
        salary = f"{r.get('min_amount') or ''}-{r.get('max_amount') or ''} {r.get('currency') or ''} {r.get('interval') or ''}".strip()
    loc = r.get("location") or ""
    return {
        "site": str(r.get("site") or ""),
        "job_url": r.get("job_url"),
        "apply_url": apply_url,
        "title": (r.get("title") or "").strip(),
        "company": (r.get("company") or "").strip(),
        "location": str(loc),
        "description": r.get("description") or "",
        "is_remote": int(bool(r.get("is_remote"))),
        "salary": salary,
        "date_posted": str(r.get("date_posted") or ""),
        "ats": detect_ats(apply_url),
    }


def passes_filters(job: dict[str, Any], search_cfg: dict[str, Any]) -> bool:
    if not job["title"] or not job["company"]:
        return False
    company = job["company"].lower()
    if any(c.lower() in company for c in search_cfg.get("exclude_companies", [])):
        return False
    title = job["title"].lower()
    return not any(w.lower() in title for w in search_cfg.get("exclude_title_words", []))


def run_searches(search_cfg: dict[str, Any]) -> Iterable[dict[str, Any]]:
    try:
        from jobspy import scrape_jobs
    except ImportError as e:
        raise SystemExit("python-jobspy is not installed. Run: pip install --no-deps python-jobspy "
                         "&& pip install 'hireloop[discover]'") from e
    for term in search_cfg["terms"]:
        for loc in search_cfg["locations"] or [""]:
            remote = loc.strip().lower() == "remote"
            log.info("searching %r in %r", term, loc or "anywhere")
            try:
                df = scrape_jobs(
                    site_name=search_cfg["sites"],
                    search_term=term,
                    google_search_term=f"{term} jobs {'remote' if remote else 'near ' + loc}".strip(),
                    location=None if remote else (loc or None),
                    is_remote=remote,
                    results_wanted=search_cfg["results_per_search"],
                    hours_old=search_cfg["hours_old"],
                    country_indeed=search_cfg["country_indeed"],
                    fetch_description=True,
                    verbose=0,
                )
            except Exception as e:  # one broken board must not stop the run
                log.warning("search failed for %r/%r: %s", term, loc, e)
                continue
            for row in df.to_dict("records"):
                yield normalize_row(row)


def discover(store: Store, cfg) -> dict[str, int]:
    stats = {"seen": 0, "new": 0, "filtered": 0}
    for job in run_searches(cfg["search"]):
        stats["seen"] += 1
        if not passes_filters(job, cfg["search"]):
            stats["filtered"] += 1
            continue
        job["fingerprint"] = fingerprint(job["company"], job["title"], job["location"])
        _, created = store.upsert_job(job)
        stats["new"] += int(created)
    return stats


class FitVerdict(BaseModel):
    score: int = Field(ge=0, le=100, description="how well the candidate fits, 0-100")
    seniority_match: bool
    dealbreakers: list[str] = Field(description="hard blockers: required years, clearance, language, location")
    reasons: list[str] = Field(description="up to 4 short reasons for the score")


FIT_SYSTEM = """You screen job postings for a candidate. Score fit honestly from 0-100.
90+: clearly qualified, most requirements met. 70-89: good fit with minor gaps.
50-69: stretch. <50: poor fit or blocked. Penalize hard requirements the candidate lacks
(years of experience well above theirs, mandatory clearances/languages/degrees, on-site in
a location they don't list). Don't reward keyword overlap alone."""


def score_jobs(store: Store, cfg, master: Resume, llm: LLM | None, limit: int = 200) -> dict[str, int]:
    resume_text = master.to_text()
    min_fit = cfg["matching"]["min_fit_score"]
    stats = {"scored": 0, "shortlisted": 0, "llm_errors": 0}
    for job in store.jobs(["discovered"], order="created_at DESC")[:limit]:
        jd = f"{job['title']}\n{job['description'] or ''}"
        kscore, _, missing = keyword_score(resume_text, jd, exclude=[job["company"] or "", job["location"] or ""])
        fit, reasons = kscore, [f"keyword match {kscore}%"]
        threshold = cfg["matching"].get("min_keyword_score", 40)
        if llm and cfg["matching"]["use_llm_scoring"] and job["description"]:
            try:
                v = llm.structured(
                    FIT_SYSTEM,
                    f"CANDIDATE:\n{master.to_prompt()}\n\nCandidate location: {master.contact.get('location', '')}\n\n"
                    f"JOB: {job['title']} at {job['company']} ({job['location']})\n{(job['description'] or '')[:12000]}",
                    FitVerdict, effort=cfg["llm"].get("classify_effort", "low"), max_tokens=4000,
                )
                fit = v.score if not v.dealbreakers else min(v.score, 40)
                threshold = min_fit
                reasons = v.reasons + [f"dealbreaker: {d}" for d in v.dealbreakers] + [f"keyword match {kscore}%"]
            except LLMUnavailable as e:
                stats["llm_errors"] += 1
                log.warning("LLM scoring failed for job %s: %s", job["id"], e)
        store.update_job(job["id"], fit_score=fit, fit_reasons=reasons, keyword_score=kscore,
                         missing_keywords=missing[:20])
        stats["scored"] += 1
        if fit >= threshold:
            store.set_status(job["id"], "shortlisted", source="scoring")
            stats["shortlisted"] += 1
    return stats
