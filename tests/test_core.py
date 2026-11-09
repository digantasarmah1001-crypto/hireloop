import pytest

from hireloop import followup
from hireloop.ats import detect_ats, extract_keywords, keyword_score
from hireloop.db import fingerprint
from hireloop.inbox import match_job, process_email, rule_classify
from hireloop.tailor import BulletChoice, RoleChoice, TailorPlan, _keyword_tailor, tailor, validate_plan


# ---------- ATS ----------
@pytest.mark.parametrize("url,ats", [
    ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://jobs.lever.co/acme/abc", "lever"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1", "workday"),
    ("https://www.linkedin.com/jobs/view/1", "linkedin"),
    ("https://careers.acme.com/1", "company_site"),
    (None, "unknown"),
])
def test_detect_ats(url, ats):
    assert detect_ats(url) == ats


def test_keywords_and_score(master, job_desc):
    kws = extract_keywords(job_desc)
    for k in ("postgresql", "kafka", "kubernetes", "fastapi", "aws", "go", "ci/cd", "rest api"):
        assert k in kws, k
    score, matched, missing = keyword_score(master.to_text(), job_desc)
    assert 30 < score < 100
    assert "kafka" in matched and "graphql" in missing


# ---------- tailoring guard ----------
def _plan(**over):
    base = dict(
        summary="Backend engineer building Python and Go services and data pipelines on AWS.",
        skills_priority=["Kafka", "PostgreSQL", "Rust"],  # Rust is not in the resume
        roles=[RoleChoice(role_id="acme", bullets=[
            BulletChoice(bullet_id="acme-3", text="Designed Kafka event pipeline feeding PostgreSQL analytics tables."),
            BulletChoice(bullet_id="acme-1", text="Built FastAPI order service handling 5M requests/day."),  # inflated number
            BulletChoice(bullet_id="startup-1", text="stolen from another role"),
            BulletChoice(bullet_id="made-up", text="Led a team of 20 engineers."),
        ])],
        project_ids=["proj-scraper", "proj-fake"],
        gaps=["GraphQL"],
        cover_letter="I'd love to join.",
    )
    base.update(over)
    return TailorPlan(**base)


def test_guard_blocks_fabrication(master):
    data, warns = validate_plan(_plan(), master)
    acme = data["experience"][0]
    assert acme["bullets"][0].startswith("Designed Kafka event pipeline")
    # inflated metric reverted to original wording
    assert "2M requests/day" in acme["bullets"][1] and "5M" not in " ".join(acme["bullets"])
    flat = str(data)
    assert "Led a team" not in flat and "stolen" not in flat and "Rust" not in flat
    assert [p["name"] for p in data["projects"]] == ["Price Tracker"]
    # role omitted by the plan is kept unchanged
    assert data["experience"][1]["company"] == "Tiny Startup" and len(data["experience"][1]["bullets"]) == 2
    assert data["skills"]["data"][0] == "Kafka"
    assert any("Rust" in w for w in warns) and any("made-up" in w for w in warns)


def test_guard_rejects_new_tech_in_summary(master):
    data, warns = validate_plan(_plan(summary="Expert in Rust, Scala and GraphQL."), master)
    assert data["summary"] == master.summary
    assert any("summary" in w for w in warns)


class FakeLLM:
    def __init__(self, result):
        self.result = result

    def structured(self, system, prompt, schema, **kw):
        assert schema is type(self.result)
        return self.result


def test_tailor_end_to_end(master, job_desc, tmp_path):
    job = {"id": 7, "title": "Backend Engineer", "company": "Example Co", "description": job_desc}
    res = tailor(job, master, FakeLLM(_plan(cover_letter="I build Kafka pipelines on AWS.")), tmp_path)
    assert res["resume_path"].endswith(".docx")
    assert (tmp_path / "7-example-co-backend-engineer" / "report.txt").exists()
    assert res["score_after"] >= res["score_before"] - 5


def test_tailor_offline_fallback(master, job_desc, tmp_path):
    res = tailor({"id": 1, "title": "Backend", "company": "X", "description": job_desc}, master, None, tmp_path)
    assert res["resume_path"] and any("unavailable" in w for w in res["warnings"])
    data = _keyword_tailor(master, job_desc)
    assert len(data["experience"][0]["bullets"]) == 3


# ---------- status machine ----------
def _seed(store, company="Example Co", title="Backend Engineer", status="applied"):
    jid, _ = store.upsert_job({"company": company, "title": title, "location": "Bengaluru",
                               "fingerprint": fingerprint(company, title)})
    store.set_status(jid, status, force=True)
    return jid


def test_status_only_moves_forward(store):
    jid = _seed(store, status="interview")
    assert store.set_status(jid, "in_review") is False  # late "we got your application" email
    assert store.get_job(jid)["status"] == "interview"
    assert store.set_status(jid, "rejected") is True
    assert store.set_status(jid, "offer") is False  # final states are sticky
    assert store.set_status(jid, "offer", force=True) is True  # user override


def test_dedupe(store):
    a, created_a = store.upsert_job({"company": "Acme", "title": "SWE", "location": "X"})
    b, created_b = store.upsert_job({"company": "ACME ", "title": "swe", "location": "x"})
    assert a == b and created_a and not created_b


# ---------- inbox ----------
@pytest.mark.parametrize("subject,body,cat", [
    ("Thank you for applying to Example Co", "We have received your application.", "application_received"),
    ("Your application", "Unfortunately, we have decided to move forward with other candidates.", "rejection"),
    ("Interview invitation", "We'd like to schedule an interview. Please pick a slot on calendly.com/x", "interview"),
    ("Next steps", "Please complete the HackerRank coding challenge within 5 days.", "assessment"),
    ("Great news", "We are pleased to extend an offer of employment.", "offer"),
    ("Weekly digest", "Top stories this week", None),
])
def test_rule_classify(subject, body, cat):
    assert rule_classify(subject, body) == cat


def test_email_updates_matching_job(store):
    jid = _seed(store)
    other = _seed(store, company="Globex", title="Data Engineer")
    res = process_email(store, None, message_id="<1@x>", sender="Example Co Recruiting <no-reply@greenhouse.io>",
                        subject="Example Co: interview for Backend Engineer",
                        body="Hi! We'd like to schedule an interview with the Example Co team.", date="2026-11-01T10:00:00+00:00")
    assert res["job_id"] == jid and res["status_changed"]
    assert store.get_job(jid)["status"] == "interview"
    assert store.get_job(other)["status"] == "applied"
    # same email again is ignored
    assert process_email(store, None, message_id="<1@x>", sender="x@greenhouse.io", subject="s",
                         body="interview", date="2026-11-01") is None


def test_email_domain_match(store):
    jid = _seed(store, company="Globex Corporation", title="Data Engineer")
    assert match_job(store, "talent@globex.com", "Update on your application", "Hello") == jid
    assert match_job(store, "news@unrelated.com", "Hello", "Nothing here") is None


# ---------- outcome loop ----------
def test_checkins_and_answers(store, cfg):
    jid = _seed(store, status="offer")
    old = _seed(store, company="Initech", title="SWE")
    store.update_job(old, last_activity_at="2026-01-01T00:00:00+00:00")
    assert followup.generate_checkins(store, cfg) == 2
    assert followup.generate_checkins(store, cfg) == 0  # no duplicates
    by_job = {c["job_id"]: c for c in store.pending_checkins()}
    assert "Did you accept" in by_job[jid]["question"]
    assert followup.apply_answer(store, by_job[jid]["id"], "accepted") == "accepted"
    assert store.get_job(jid)["status"] == "accepted"
    assert followup.apply_answer(store, by_job[old]["id"], "no news") is None
    assert store.get_job(old)["status"] == "applied"
