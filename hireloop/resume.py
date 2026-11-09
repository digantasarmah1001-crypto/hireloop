"""Master resume model + ATS-friendly DOCX rendering.

ATS-friendly means: single column, no tables/text boxes/headers/footers for
content, standard section names, plain bullets, a real .docx (most ATS parse
docx more reliably than designed PDFs).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Bullet(BaseModel):
    id: str
    text: str


class Role(BaseModel):
    id: str
    company: str
    title: str
    location: str = ""
    start: str = ""
    end: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    bullets: list[Bullet] = Field(default_factory=list)


class Education(BaseModel):
    school: str
    degree: str
    start: str = ""
    end: str = ""


class Resume(BaseModel):
    name: str
    headline: str = ""
    contact: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    skills: dict[str, list[str]] = Field(default_factory=dict)
    experience: list[Role] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Resume":
        data = yaml.safe_load(Path(path).read_text())
        for sec in ("education",):
            for e in data.get(sec, []) or []:
                for k in ("start", "end"):
                    if k in e and e[k] is not None:
                        e[k] = str(e[k])
        for r in data.get("experience", []) or []:
            for k in ("start", "end"):
                if k in r and r[k] is not None:
                    r[k] = str(r[k])
        return cls.model_validate(data)

    def all_bullets(self) -> dict[str, str]:
        out = {}
        for r in self.experience:
            for b in r.bullets:
                out[b.id] = b.text
        for p in self.projects:
            for b in p.bullets:
                out[b.id] = b.text
        return out

    def all_skills(self) -> list[str]:
        return [s for group in self.skills.values() for s in group]

    def to_text(self) -> str:
        lines = [self.name, self.headline, self.summary]
        lines.append("Skills: " + ", ".join(self.all_skills()))
        for r in self.experience:
            lines.append(f"{r.title} at {r.company} ({r.start} - {r.end})")
            lines += [f"- {b.text}" for b in r.bullets]
        for p in self.projects:
            lines.append(f"Project: {p.name}")
            lines += [f"- {b.text}" for b in p.bullets]
        for e in self.education:
            lines.append(f"{e.degree}, {e.school} ({e.start}-{e.end})")
        lines += self.certifications
        return "\n".join(x for x in lines if x)

    def to_prompt(self) -> str:
        """Resume with bullet ids, for the LLM to reference."""
        out = [f"NAME: {self.name}", f"HEADLINE: {self.headline}", f"SUMMARY: {self.summary}", "SKILLS:"]
        for g, items in self.skills.items():
            out.append(f"  {g}: {', '.join(items)}")
        out.append("EXPERIENCE:")
        for r in self.experience:
            out.append(f"  [{r.id}] {r.title} @ {r.company}, {r.location} ({r.start} – {r.end})")
            out += [f"    ({b.id}) {b.text}" for b in r.bullets]
        if self.projects:
            out.append("PROJECTS:")
            for p in self.projects:
                out.append(f"  [{p.id}] {p.name}")
                out += [f"    ({b.id}) {b.text}" for b in p.bullets]
        out.append("EDUCATION:")
        out += [f"  {e.degree}, {e.school} ({e.start}–{e.end})" for e in self.education]
        if self.certifications:
            out.append("CERTIFICATIONS: " + "; ".join(self.certifications))
        return "\n".join(out)


def render_docx(data: dict[str, Any], path: str | Path) -> Path:
    """Render a (possibly tailored) resume dict to an ATS-friendly .docx."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    for s in doc.sections:
        s.left_margin = s.right_margin = s.top_margin = s.bottom_margin = Pt(42)

    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = h.add_run(data["name"])
    run.bold = True
    run.font.size = Pt(16)
    c = data.get("contact", {})
    contact = " | ".join(v for v in [c.get("email"), c.get("phone"), c.get("location"),
                                     c.get("linkedin"), c.get("github")] if v)
    p = doc.add_paragraph(contact)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    def heading(text: str) -> None:
        para = doc.add_paragraph()
        r = para.add_run(text.upper())
        r.bold = True
        r.font.size = Pt(11.5)
        para.paragraph_format.space_before = Pt(8)
        para.paragraph_format.space_after = Pt(2)

    def bullet(text: str) -> None:
        para = doc.add_paragraph(text, style="List Bullet")
        para.paragraph_format.space_after = Pt(1)

    if data.get("summary"):
        heading("Summary")
        doc.add_paragraph(data["summary"])
    if data.get("skills"):
        heading("Skills")
        for group, items in data["skills"].items():
            if items:
                para = doc.add_paragraph()
                para.add_run(group.replace("_", " ").title() + ": ").bold = True
                para.add_run(", ".join(items))
                para.paragraph_format.space_after = Pt(0)
    if data.get("experience"):
        heading("Experience")
        for r in data["experience"]:
            para = doc.add_paragraph()
            para.add_run(f"{r['title']}, {r['company']}").bold = True
            loc = f" — {r['location']}" if r.get("location") else ""
            para.add_run(f"{loc}   {r.get('start', '')} – {r.get('end', '')}")
            para.paragraph_format.space_after = Pt(1)
            for b in r["bullets"]:
                bullet(b)
    if data.get("projects"):
        heading("Projects")
        for pr in data["projects"]:
            doc.add_paragraph().add_run(pr["name"]).bold = True
            for b in pr["bullets"]:
                bullet(b)
    if data.get("education"):
        heading("Education")
        for e in data["education"]:
            doc.add_paragraph(f"{e['degree']}, {e['school']} ({e.get('start', '')}–{e.get('end', '')})")
    if data.get("certifications"):
        heading("Certifications")
        for cert in data["certifications"]:
            bullet(cert)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def resume_as_dict(r: Resume) -> dict[str, Any]:
    """Untailored resume in the render_docx shape."""
    return {
        "name": r.name, "contact": r.contact, "summary": r.summary, "skills": r.skills,
        "experience": [{**x.model_dump(exclude={"bullets", "id"}), "bullets": [b.text for b in x.bullets]}
                       for x in r.experience],
        "projects": [{"name": p.name, "bullets": [b.text for b in p.bullets]} for p in r.projects],
        "education": [e.model_dump() for e in r.education],
        "certifications": r.certifications,
    }
