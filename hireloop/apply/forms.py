"""Label-driven form filling that works across ATS platforms.

Instead of one hard-coded selector set per site (what breaks most bots), we
read each field's label and map it to a profile value. Greenhouse, Lever,
Ashby, Workable and many company sites use plain labelled forms, so one
filler covers them; site-specific code only handles navigation quirks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

# (regex on label, profile key). First match wins, so specific before generic.
FIELD_RULES: list[tuple[str, str]] = [
    (r"first\s*name|given\s*name", "first_name"),
    (r"last\s*name|surname|family\s*name", "last_name"),
    (r"preferred\s*name", "first_name"),
    (r"^(full\s*)?name\b|your name", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile|contact number", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"portfolio|personal (web)?site|website", "portfolio"),
    (r"(current )?location|city|where are you based", "location"),
    (r"current (company|employer)|present employer", "current_company"),
    (r"(authori[sz]ed|eligible|legally).{0,40}work|work authori[sz]ation|right to work", "work_authorization"),
    (r"sponsor", "requires_sponsorship"),
    (r"notice period|when can you start|start date|earliest", "notice_period"),
    (r"salary|compensation|ctc|pay expectation", "expected_salary"),
    (r"relocat", "willing_to_relocate"),
]
RESUME_RE = re.compile(r"resume|cv\b|curriculum", re.I)
COVER_RE = re.compile(r"cover\s*letter", re.I)
# Questions we never auto-answer: voluntary self-identification (EEO etc.).
SENSITIVE_RE = re.compile(r"gender|race|ethnic|veteran|disabilit|sexual orientation|pronoun|religion|caste", re.I)


@dataclass
class FillReport:
    filled: dict[str, str] = field(default_factory=dict)
    drafted: dict[str, str] = field(default_factory=dict)   # LLM-written, needs review
    skipped_sensitive: list[str] = field(default_factory=list)
    unfilled_required: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)

    def needs_review(self) -> bool:
        return bool(self.drafted or self.unfilled_required)


def profile_values(profile: dict[str, Any], current_company: str = "") -> dict[str, str]:
    v = {k: str(profile.get(k) or "") for k in
         ("first_name", "last_name", "email", "phone", "location", "linkedin", "github", "portfolio")}
    v["full_name"] = f"{v['first_name']} {v['last_name']}".strip()
    v["current_company"] = current_company
    v.update({k: str(x) for k, x in (profile.get("answers") or {}).items()})
    return v


def classify_label(label: str) -> str | None:
    for pat, key in FIELD_RULES:
        if re.search(pat, label, re.I):
            return key
    return None


# JS that lists fillable fields with their best-guess label.
_FIELDS_JS = r"""
() => {
  const out = [];
  const els = document.querySelectorAll('input, textarea, select');
  let i = 0;
  for (const el of els) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden','submit','button','image','reset','search'].includes(type)) continue;
    const style = window.getComputedStyle(el);
    if (type !== 'file' && (style.display === 'none' || style.visibility === 'hidden')) continue;
    let label = el.getAttribute('aria-label') || '';
    if (!label && el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = l.innerText;
    }
    if (!label) { const l = el.closest('label'); if (l) label = l.innerText; }
    if (!label) {
      const wrap = el.closest('.field, .application-question, .form-group, li, div');
      if (wrap) label = (wrap.querySelector('label, .application-label, legend') || wrap).innerText || '';
    }
    if (!label) label = el.getAttribute('placeholder') || el.getAttribute('name') || '';
    const key = 'hl-' + (i++);
    el.setAttribute('data-hl', key);
    out.push({
      key, type, tag: el.tagName.toLowerCase(), name: el.getAttribute('name') || '',
      label: label.trim().slice(0, 300),
      required: el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label),
      value: el.value || '',
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text.trim()) : [],
    });
  }
  return out;
}
"""


def _pick_option(options: list[str], wanted: str) -> str | None:
    w = wanted.lower().strip()
    if not w:
        return None
    for o in options:
        if o.lower().strip() == w:
            return o
    for o in options:
        if w in o.lower() or (o.lower().strip() and o.lower().strip() in w):
            return o
    if w in ("yes", "no"):
        for o in options:
            if o.lower().startswith(w):
                return o
    return None


def fill_form(page, values: dict[str, str], resume_path: str | None, cover_path: str | None,
              draft_answer: Callable[[str, list[str]], str] | None = None) -> FillReport:
    """Fill every field we can on the current page. Never submits."""
    rep = FillReport()
    fields = page.evaluate(_FIELDS_JS)
    for f in fields:
        loc = page.locator(f'[data-hl="{f["key"]}"]')
        label = f["label"] or f["name"]
        short = label.split("\n")[0][:80]
        if f["type"] == "file":
            if COVER_RE.search(label):
                target, kind = cover_path, "cover_letter"
            elif RESUME_RE.search(label) or "resume" not in rep.uploaded:
                target, kind = resume_path, "resume"  # unlabelled first file input = resume
            else:
                target, kind = None, ""
            if target:
                try:
                    loc.set_input_files(target)
                    rep.uploaded.append(kind)
                except Exception:
                    if f["required"]:
                        rep.unfilled_required.append(short)
            continue
        if f["value"] and f["type"] not in ("checkbox", "radio"):
            continue  # already filled (e.g. pre-filled by the site)
        if SENSITIVE_RE.search(label):
            rep.skipped_sensitive.append(short)
            continue
        key = classify_label(label)
        value = values.get(key, "") if key else ""
        if f["type"] in ("checkbox", "radio"):
            # Only tick consent / privacy acknowledgements that are required.
            if f["required"] and re.search(r"agree|consent|acknowledge|privacy|terms", label, re.I):
                loc.check()
                rep.filled[short] = "checked"
            elif f["required"]:
                rep.unfilled_required.append(short)
            continue
        if not value and draft_answer and (f["required"] or f["tag"] == "textarea"):
            try:
                value = draft_answer(label, f["options"])
                if value:
                    rep.drafted[short] = value
            except Exception:
                value = ""
        if not value:
            if f["required"]:
                rep.unfilled_required.append(short)
            continue
        try:
            if f["tag"] == "select":
                opt = _pick_option(f["options"], value)
                if not opt:
                    if f["required"]:
                        rep.unfilled_required.append(short)
                    rep.drafted.pop(short, None)
                    continue
                loc.select_option(label=opt)
                value = opt
            else:
                loc.fill(value)
            if short not in rep.drafted:
                rep.filled[short] = value
        except Exception:
            if f["required"]:
                rep.unfilled_required.append(short)
    return rep
