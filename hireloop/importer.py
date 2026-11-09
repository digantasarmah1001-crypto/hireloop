"""Turn an existing PDF/DOCX/TXT resume into the structured resume.yaml."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .llm import LLM


class _Bullet(BaseModel):
    text: str


class _Role(BaseModel):
    company: str
    title: str
    location: str
    start: str = Field(description="YYYY-MM or YYYY")
    end: str = Field(description="YYYY-MM, YYYY or 'present'")
    bullets: list[_Bullet]


class _Project(BaseModel):
    name: str
    bullets: list[_Bullet]


class _Edu(BaseModel):
    school: str
    degree: str
    start: str
    end: str


class _SkillGroup(BaseModel):
    group: str = Field(description="e.g. languages, frameworks, cloud_devops, data, tools")
    items: list[str]


class _Parsed(BaseModel):
    name: str
    headline: str
    email: str
    phone: str
    location: str
    linkedin: str
    github: str
    summary: str
    skills: list[_SkillGroup]
    experience: list[_Role]
    projects: list[_Project]
    education: list[_Edu]
    certifications: list[str]


def read_text(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".pdf":
        from pypdf import PdfReader

        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    if suf == ".docx":
        from docx import Document

        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    return path.read_text()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:20] or "x"


def import_resume(src: Path, dest: Path, llm: LLM) -> Path:
    text = read_text(src)
    p = llm.structured(
        "Convert this resume into structured data. Copy facts and bullet wording exactly; "
        "do not improve, summarize or invent anything. Empty string for missing fields.",
        text[:40000], _Parsed)
    used: set[str] = set()

    def uid(base: str) -> str:
        i, cand = 1, base
        while cand in used:
            i += 1
            cand = f"{base}{i}"
        used.add(cand)
        return cand

    exp = []
    for r in p.experience:
        rid = uid(_slug(r.company))
        exp.append({"id": rid, "company": r.company, "title": r.title, "location": r.location,
                    "start": r.start, "end": r.end,
                    "bullets": [{"id": f"{rid}-{i + 1}", "text": b.text} for i, b in enumerate(r.bullets)]})
    projects = []
    for pr in p.projects:
        pid = uid("proj-" + _slug(pr.name))
        projects.append({"id": pid, "name": pr.name,
                         "bullets": [{"id": f"{pid}-{i + 1}", "text": b.text} for i, b in enumerate(pr.bullets)]})
    data = {
        "name": p.name, "headline": p.headline,
        "contact": {k: v for k, v in {"email": p.email, "phone": p.phone, "location": p.location,
                                      "linkedin": p.linkedin, "github": p.github}.items() if v},
        "summary": p.summary,
        "skills": {g.group: g.items for g in p.skills},
        "experience": exp, "projects": projects,
        "education": [e.model_dump() for e in p.education],
        "certifications": p.certifications,
    }
    dest.write_text("# Imported by hireloop — review every line; this is the source of truth.\n"
                    + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110))
    return dest
