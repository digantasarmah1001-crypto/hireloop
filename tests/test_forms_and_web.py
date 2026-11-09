from pathlib import Path

import pytest

from hireloop.apply.forms import classify_label, fill_form, profile_values

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("label,key", [
    ("First Name *", "first_name"), ("Last name", "last_name"), ("Email address", "email"),
    ("Mobile number", "phone"), ("LinkedIn Profile", "linkedin"),
    ("Are you legally authorized to work in India?", "work_authorization"),
    ("Will you require visa sponsorship?", "requires_sponsorship"),
    ("Expected CTC", "expected_salary"), ("Favourite colour", None),
])
def test_classify_label(label, key):
    assert classify_label(label) == key


def test_fill_form_in_browser(cfg, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    resume = tmp_path / "r.docx"
    resume.write_bytes(b"x")
    cover = tmp_path / "c.txt"
    cover.write_text("hi")
    asked = []

    def drafter(question, options):
        asked.append(question)
        return "I like payments infrastructure." if "Why" in question else ""

    with pw.sync_playwright() as p:
        from hireloop.apply.runner import launch_browser

        b = launch_browser(p, True)
        page = b.new_page()
        page.goto((FIX / "greenhouse_form.html").as_uri())
        rep = fill_form(page, profile_values(cfg["profile"]), str(resume), str(cover), drafter)
        assert page.input_value("#first_name") == "Your"
        assert page.input_value("#email") == "you@example.com"
        assert page.input_value("#sp") == "No"
        assert page.input_value("#gender") == ""          # never answered
        assert page.is_checked("#consent")
        assert page.input_value("#why") == "I like payments infrastructure."
        assert set(rep.uploaded) == {"resume", "cover_letter"}
        assert "Gender" in rep.skipped_sensitive
        assert rep.drafted and rep.needs_review()       # drafted answer blocks auto-submit
        assert page.locator("text=Thank you").count() == 0  # never submits by itself
        b.close()


def test_dashboard(cfg, tmp_path):
    from fastapi.testclient import TestClient

    from hireloop.db import Store
    from hireloop.web import create_app

    s = Store(cfg.resolve(cfg["db_path"]))
    jid, _ = s.upsert_job({"company": "Acme", "title": "SWE", "location": ""})
    s.set_status(jid, "offer", force=True)
    c = TestClient(create_app(str(cfg.path)))
    r = c.get("/")
    assert r.status_code == 200 and "Did you accept" in r.text
    cid = s.pending_checkins()[0]["id"]
    assert c.post(f"/checkin/{cid}", data={"answer": "accepted"}, follow_redirects=False).status_code == 303
    assert s.get_job(jid)["status"] == "accepted"
    assert c.get(f"/job/{jid}").status_code == 200
