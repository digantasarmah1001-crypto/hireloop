"""Drive a browser to an application form, fill it, and (optionally) submit.

Policy, on purpose:
- Default mode is REVIEW: the form is filled and left open; you check it and
  click Submit yourself. hireloop watches for the submission and records it.
- AUTO mode submits only on Greenhouse/Lever-style forms, and only when every
  required field was filled from your profile (nothing LLM-drafted).
- LinkedIn/Indeed/Naukri in-platform "easy apply" is not automated: those
  sites prohibit bots and ban accounts. hireloop opens the posting with your
  tailored files ready instead.
- CAPTCHAs are never bypassed; you solve them in the open window.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .forms import FillReport, fill_form, profile_values

AUTO_OK = {"greenhouse", "lever", "ashby"}
NO_AUTOMATION = {"linkedin", "indeed", "naukri", "glassdoor"}

APPLY_BUTTON = re.compile(r"^\s*(apply( now| for this job)?|i'?m interested)\s*$", re.I)
SUBMIT_BUTTON = re.compile(r"submit( application)?|send application|apply", re.I)
SUCCESS_TEXT = re.compile(r"thank you for (applying|your application)|application (was )?(submitted|received)|"
                          r"we('ve| have) received your application", re.I)


class Answer(BaseModel):
    answer: str = Field(description="the answer to put in the form; empty if it can't be answered "
                                    "truthfully from the candidate info")


def make_drafter(llm, resume_prompt: str, job: dict[str, Any], profile: dict[str, Any]) -> Callable:
    def draft(question: str, options: list[str]) -> str:
        if llm is None:
            return ""
        opts = f"\nChoose exactly one of: {options}" if options else ""
        a = llm.structured(
            "You fill job application questions for a candidate, truthfully, from their resume and "
            "profile only. Be concise (under 120 words for free text). If the info isn't there, "
            "return an empty answer — never guess facts.",
            f"CANDIDATE PROFILE: {json.dumps(profile.get('answers', {}))}\nRESUME:\n{resume_prompt}\n\n"
            f"JOB: {job['title']} at {job['company']}\n\nQUESTION: {question}{opts}",
            Answer, max_tokens=2000,
        )
        return a.answer.strip()
    return draft


def launch_browser(p, headless: bool):
    """Use HIRELOOP_CHROMIUM to point at a specific Chrome/Chromium binary if needed."""
    exe = os.environ.get("HIRELOOP_CHROMIUM")
    return p.chromium.launch(headless=headless, executable_path=exe or None)


def _click_apply_if_needed(page) -> None:
    """Lever/Greenhouse job pages sometimes show the form behind an Apply button."""
    if page.locator("input[type=file]").count() > 0:
        return
    for el in page.get_by_role("link").all() + page.get_by_role("button").all():
        try:
            if APPLY_BUTTON.match(el.inner_text(timeout=500) or ""):
                el.click()
                page.wait_for_load_state("domcontentloaded")
                return
        except Exception:
            continue


def run_application(job: dict[str, Any], cfg, resume_path: str, cover_path: str | None,
                    drafter: Callable | None, *, current_company: str = "",
                    wait_for_user_seconds: int = 900) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    ats = job.get("ats") or "unknown"
    mode = cfg["apply"]["mode"]
    url = job.get("apply_url") or job.get("job_url")
    shots = Path(cfg.resolve(cfg["output_dir"])) / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"job_id": job.get("id"), "ats": ats, "url": url, "submitted": False}

    headless = cfg["apply"]["headless"] and mode == "auto"
    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        if ats in NO_AUTOMATION:
            result["note"] = (f"{ats} doesn't allow automated applying. The posting is open; "
                              f"upload {resume_path} yourself.")
            report = FillReport()
        else:
            _click_apply_if_needed(page)
            report = fill_form(page, profile_values(cfg["profile"], current_company), resume_path,
                               cover_path, drafter)
        result["fill"] = report.__dict__
        page.screenshot(path=str(shots / f"{job.get('id')}-filled.png"), full_page=True)

        can_auto = (mode == "auto" and ats in AUTO_OK and not report.needs_review()
                    and "resume" in report.uploaded)
        if can_auto:
            btn = page.get_by_role("button", name=SUBMIT_BUTTON).last
            btn.click()
            page.wait_for_timeout(5000)
            result["submitted"] = bool(SUCCESS_TEXT.search(page.content()))
            page.screenshot(path=str(shots / f"{job.get('id')}-submitted.png"), full_page=True)
        elif not headless:
            # Review mode: user checks and clicks Submit. Detect success text or window close.
            print("\n  Review the form in the browser window and click Submit when it looks right.")
            if report.drafted:
                print("  AI-drafted answers to double-check:", ", ".join(report.drafted))
            if report.unfilled_required:
                print("  Required fields left for you:", ", ".join(report.unfilled_required))
            deadline = time.time() + wait_for_user_seconds
            while time.time() < deadline:
                try:
                    if SUCCESS_TEXT.search(page.content()):
                        result["submitted"] = True
                        break
                    page.wait_for_timeout(1500)
                except Exception:  # window closed
                    break
        browser.close()
    return result
