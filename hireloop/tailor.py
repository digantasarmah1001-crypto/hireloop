"""Per-job resume tailoring with an enforced no-fabrication guard.

Most auto-apply tools ask the LLM nicely not to lie, then trust the output.
Here the LLM never writes the resume directly. It returns a *plan*:
  - which existing bullets to keep, in what order, each optionally reworded
  - which existing skills to surface first
  - a rewritten summary
Code then builds the resume from the master copy and rejects anything that
references a bullet, skill, employer or title not in the master resume.
Rewordings are checked for introduced numbers and unknown tech terms.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .ats import KNOWN_TERMS, _norm, extract_keywords, keyword_score
from .llm import LLM, LLMUnavailable
from .resume import Resume, render_docx, resume_as_dict


class BulletChoice(BaseModel):
    bullet_id: str = Field(description="id of an existing bullet from the master resume")
    text: str = Field(description="the bullet, optionally reworded to mirror the job's wording; "
                                  "same facts, same numbers, no new claims")


class RoleChoice(BaseModel):
    role_id: str
    bullets: list[BulletChoice]


class TailorPlan(BaseModel):
    summary: str = Field(description="2-3 sentence summary aimed at this job, using only facts in the resume")
    skills_priority: list[str] = Field(description="existing skills, most relevant to this job first")
    roles: list[RoleChoice] = Field(description="every role from the master resume, in original order")
    project_ids: list[str] = Field(description="ids of projects worth keeping for this job, best first")
    gaps: list[str] = Field(description="important job requirements the resume genuinely does not cover")
    cover_letter: str = Field(description="under 250 words, specific to the company, no clichés, "
                                          "only claims backed by the resume")


SYSTEM = """You tailor resumes so they pass ATS keyword screens and read well to a recruiter.
Hard rules — violating any of these makes the output unusable:
- Use only facts present in the master resume. Never add employers, titles, dates, degrees,
  certifications, tools, skills, metrics or numbers that are not already there.
- You may reword a bullet to use the job description's terminology for the SAME thing
  (e.g. "Postgres" → "PostgreSQL", "built APIs" → "designed REST APIs" if REST is evident).
- Reference bullets by their id. Drop bullets that are irrelevant; keep at least 2 per role.
- Put the most relevant bullets first within each role. Keep every role.
- List real gaps honestly in `gaps` instead of papering over them."""


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(s)}


def _terms(s: str) -> set[str]:
    low = " " + s.lower() + " "
    return {_norm(t) for t in KNOWN_TERMS
            if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9+#])", low)}


class FabricationError(ValueError):
    pass


def validate_plan(plan: TailorPlan, master: Resume) -> tuple[dict[str, Any], list[str]]:
    """Build the tailored resume from the plan. Returns (resume_dict, warnings).

    Bad references are dropped; rewordings that introduce new numbers or new tech
    terms fall back to the original bullet text. Raises only if the result would
    be empty/broken."""
    warnings: list[str] = []
    bullets = master.all_bullets()
    master_text = master.to_text()
    master_terms = _terms(master_text)
    known_skills = {s.lower(): s for s in master.all_skills()}

    def safe_text(bid: str, proposed: str) -> str:
        orig = bullets[bid]
        new_nums = _numbers(proposed) - _numbers(orig)
        new_terms = _terms(proposed) - master_terms
        if new_nums or new_terms:
            warnings.append(f"reworded bullet {bid} added {sorted(new_nums | new_terms)}; kept original")
            return orig
        return proposed.strip() or orig

    roles_by_id = {r.id: r for r in master.experience}
    chosen = {rc.role_id: rc for rc in plan.roles}
    experience = []
    for role in master.experience:  # keep every role, master order, master facts
        rc = chosen.get(role.id)
        picked: list[str] = []
        seen = set()
        if rc:
            own = {b.id for b in role.bullets}
            for bc in rc.bullets:
                if bc.bullet_id not in own:
                    warnings.append(f"ignored bullet {bc.bullet_id!r}: not in role {role.id}")
                    continue
                if bc.bullet_id in seen:
                    continue
                seen.add(bc.bullet_id)
                picked.append(safe_text(bc.bullet_id, bc.text))
        else:
            warnings.append(f"plan omitted role {role.id}; kept it unchanged")
        if len(picked) < 2:  # top up from master in original order
            for b in role.bullets:
                if len(picked) >= 2:
                    break
                if b.id not in seen:
                    picked.append(b.text)
                    seen.add(b.id)
        experience.append({"company": role.company, "title": role.title, "location": role.location,
                           "start": role.start, "end": role.end, "bullets": picked})
    for rid in chosen:
        if rid not in roles_by_id:
            warnings.append(f"ignored unknown role {rid!r}")

    # Skills: only existing ones, reordered by priority within their groups.
    prio = []
    for s in plan.skills_priority:
        if s.lower() in known_skills:
            prio.append(known_skills[s.lower()])
        else:
            warnings.append(f"ignored skill {s!r}: not in master resume")
    rank = {s: i for i, s in enumerate(prio)}
    skills = {g: sorted(items, key=lambda s: rank.get(s, 999)) for g, items in master.skills.items()}

    projects_by_id = {p.id: p for p in master.projects}
    projects = []
    for pid in plan.project_ids:
        if pid in projects_by_id:
            p = projects_by_id[pid]
            projects.append({"name": p.name, "bullets": [b.text for b in p.bullets]})
        else:
            warnings.append(f"ignored unknown project {pid!r}")

    summary = plan.summary.strip()
    new_nums = _numbers(summary) - _numbers(master_text)
    new_terms = _terms(summary) - master_terms
    if new_nums or new_terms:
        warnings.append(f"summary added {sorted(new_nums | new_terms)}; kept original summary")
        summary = master.summary

    out = {
        "name": master.name, "contact": master.contact, "summary": summary, "skills": skills,
        "experience": experience, "projects": projects,
        "education": [e.model_dump() for e in master.education],
        "certifications": master.certifications,
    }
    if not experience and master.experience:
        raise FabricationError("tailoring produced an empty resume")
    return out, warnings


def check_cover_letter(text: str, master: Resume) -> list[str]:
    """Flag (don't silently fix) unsupported claims in the free-text cover letter."""
    master_text = master.to_text()
    flags = []
    extra_terms = _terms(text) - _terms(master_text)
    if extra_terms:
        flags.append(f"cover letter mentions tools not in your resume: {sorted(extra_terms)}")
    # Years/percentages that don't appear in the resume
    extra_nums = {n for n in _numbers(text) - _numbers(master_text) if len(n) <= 4 and not n.startswith("20")}
    if extra_nums:
        flags.append(f"cover letter uses numbers not in your resume: {sorted(extra_nums)}")
    return flags


def tailor(job: dict[str, Any], master: Resume, llm: LLM | None, out_dir: str | Path) -> dict[str, Any]:
    """Tailor for one job. Falls back to keyword-based reordering without an LLM."""
    jd = f"{job['title']} at {job['company']}\n\n{job.get('description') or ''}"
    excl = [job.get("company") or "", job.get("location") or ""]
    before, _, _ = keyword_score(_flat(resume_as_dict(master)), jd, exclude=excl)
    warnings: list[str] = []
    gaps: list[str] = []
    cover = ""
    try:
        if llm is None:
            raise LLMUnavailable("no LLM")
        plan = llm.structured(
            SYSTEM,
            f"MASTER RESUME:\n{master.to_prompt()}\n\nJOB POSTING:\n{jd[:15000]}\n\n"
            f"ATS keywords found in the posting: {', '.join(extract_keywords(jd))}",
            TailorPlan,
        )
        data, warnings = validate_plan(plan, master)
        gaps = plan.gaps
        cover = plan.cover_letter.strip()
        warnings += check_cover_letter(cover, master)
    except LLMUnavailable as e:
        data = _keyword_tailor(master, jd)
        warnings.append(f"AI tailoring unavailable ({e}); used keyword reordering")

    flat = _flat(data)
    after, matched, missing = keyword_score(flat, jd, exclude=excl)

    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['title']}".lower()).strip("-")[:60]
    out_dir = Path(out_dir) / f"{job.get('id', 0)}-{slug}"
    resume_path = render_docx(data, out_dir / f"{master.name.replace(' ', '_')}_Resume.docx")
    cover_path = None
    if cover:
        cover_path = out_dir / "cover_letter.txt"
        cover_path.write_text(cover)
    (out_dir / "report.txt").write_text(
        f"Keyword match: {before}% → {after}%\nMissing keywords: {', '.join(missing) or '-'}\n"
        f"Gaps: {'; '.join(gaps) or '-'}\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings))
    return {"resume_path": str(resume_path), "cover_letter_path": str(cover_path) if cover_path else None,
            "score_before": before, "score_after": after, "missing": missing, "gaps": gaps,
            "warnings": warnings, "cover_letter": cover}


def _flat(data: dict[str, Any]) -> str:
    """Text of a rendered resume dict, for before/after keyword comparison."""
    return "\n".join([data["summary"], ", ".join(s for g in data["skills"].values() for s in g)]
                     + [f"{r['title']} {r['company']}" for r in data["experience"]]
                     + [b for r in data["experience"] for b in r["bullets"]]
                     + [p["name"] + " " + " ".join(p["bullets"]) for p in data["projects"]]
                     + [f"{e['degree']} {e['school']}" for e in data["education"]]
                     + list(data.get("certifications") or []))


def _keyword_tailor(master: Resume, jd: str) -> dict[str, Any]:
    """Offline fallback: reorder bullets and skills by keyword overlap. No rewording."""
    kws = set(extract_keywords(jd))

    def rel(text: str) -> int:
        return len(_terms(text) & kws) * 2 + sum(1 for k in kws if k in text.lower())

    data = resume_as_dict(master)
    for r in data["experience"]:
        r["bullets"].sort(key=rel, reverse=True)
    for g in data["skills"]:
        data["skills"][g] = sorted(data["skills"][g], key=lambda s: _norm(s) in kws, reverse=True)
    data["projects"].sort(key=lambda p: rel(" ".join(p["bullets"])), reverse=True)
    return data
