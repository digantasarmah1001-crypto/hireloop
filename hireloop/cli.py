"""hireloop command line."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from . import followup
from .config import load_config
from .db import STATUSES, Store, fingerprint
from .llm import LLM, LLMUnavailable, from_config
from .resume import Resume

PKG_ROOT = Path(__file__).resolve().parent.parent


def _ctx(args):
    cfg = load_config(args.config)
    store = Store(cfg.resolve(cfg["db_path"]))
    return cfg, store


def _llm(cfg, purpose="default") -> LLM | None:
    if not LLM.available():
        return None
    return from_config(cfg, purpose=purpose)


def _master(cfg) -> Resume:
    p = cfg.resolve(cfg["resume_path"])
    if not p.exists():
        sys.exit(f"No resume at {p}. Run `hireloop import-resume your_cv.pdf` or copy resume.example.yaml.")
    return Resume.load(p)


def cmd_init(args):
    for src, dst in (("config.example.yaml", "config.yaml"), ("resume.example.yaml", "resume.yaml")):
        d = Path(dst)
        if d.exists():
            print(f"{dst} already exists, leaving it alone")
        else:
            shutil.copy(PKG_ROOT / src, d)
            print(f"created {dst}")
    print("\nNext: edit config.yaml (profile, search terms), then either edit resume.yaml or run\n"
          "  hireloop import-resume path/to/your_cv.pdf")


def cmd_import_resume(args):
    cfg = load_config(args.config)
    llm = _llm(cfg)
    if not llm:
        sys.exit("Importing needs ANTHROPIC_API_KEY. Or fill resume.yaml by hand from resume.example.yaml.")
    from .importer import import_resume

    dest = cfg.resolve(cfg["resume_path"])
    if dest.exists() and not args.force:
        sys.exit(f"{dest} exists; pass --force to overwrite")
    import_resume(Path(args.file), dest, llm)
    print(f"Wrote {dest}. Read it through — it's what every tailored resume is built from.")


def cmd_discover(args):
    cfg, store = _ctx(args)
    from .discover import discover

    if not cfg["search"]["terms"]:
        sys.exit("Add search.terms to config.yaml first.")
    print(json.dumps(discover(store, cfg)))


def cmd_score(args):
    cfg, store = _ctx(args)
    from .discover import score_jobs

    print(json.dumps(score_jobs(store, cfg, _master(cfg), _llm(cfg, "classify"), limit=args.limit)))


def cmd_add(args):
    """Add a job by hand (e.g. one you found yourself)."""
    cfg, store = _ctx(args)
    from .ats import detect_ats

    desc = Path(args.description_file).read_text() if args.description_file else ""
    jid, created = store.upsert_job({
        "site": "manual", "job_url": args.url, "apply_url": args.url, "title": args.title,
        "company": args.company, "location": args.location or "", "description": desc,
        "ats": detect_ats(args.url), "fingerprint": fingerprint(args.company, args.title, args.location or ""),
    })
    if created:
        store.set_status(jid, "shortlisted", source="manual")
    print(f"job {jid} {'added' if created else 'already exists'}")


def _fmt_job(j) -> str:
    fit = f"{j['fit_score']:.0f}" if j["fit_score"] is not None else " -"
    return f"[{j['id']:>4}] {fit:>3}  {j['status']:<11} {j['company'][:24]:<24} {j['title'][:44]:<44} {j['ats'] or ''}"


def cmd_list(args):
    cfg, store = _ctx(args)
    statuses = args.status.split(",") if args.status else None
    for j in store.jobs(statuses)[: args.limit]:
        print(_fmt_job(j))


def cmd_show(args):
    cfg, store = _ctx(args)
    j = store.get_job(args.job)
    if not j:
        sys.exit("no such job")
    print(_fmt_job(j))
    print(f"  {j['apply_url']}\n  location: {j['location']}  posted: {j['date_posted']}")
    if j["fit_reasons"]:
        for r in json.loads(j["fit_reasons"]):
            print(f"  · {r}")
    if j["missing_keywords"]:
        print(f"  missing keywords: {', '.join(json.loads(j['missing_keywords']))}")
    if j["resume_path"]:
        print(f"  resume: {j['resume_path']}")
    for e in store.events(j["id"]):
        print(f"  {e['occurred_at'][:16]}  {e['kind']:<13} {e['summary'] or e['subject']}")


def _target_jobs(store, args, statuses):
    if args.job:
        j = store.get_job(args.job)
        return [j] if j else []
    return store.jobs(statuses)[: args.top]


def cmd_tailor(args):
    cfg, store = _ctx(args)
    from .tailor import tailor

    master, llm = _master(cfg), _llm(cfg)
    out = cfg.resolve(cfg["output_dir"])
    for j in _target_jobs(store, args, ["shortlisted"]):
        res = tailor(dict(j), master, llm, out)
        store.update_job(j["id"], resume_path=res["resume_path"], cover_letter_path=res["cover_letter_path"],
                         missing_keywords=res["missing"][:20])
        store.set_status(j["id"], "tailored", source="tailor")
        print(f"{_fmt_job(j)}\n   ATS keyword match {res['score_before']}% → {res['score_after']}%  "
              f"→ {res['resume_path']}")
        for w in res["warnings"]:
            print(f"   ! {w}")
        if res["gaps"]:
            print(f"   gaps: {'; '.join(res['gaps'])}")


def cmd_apply(args):
    cfg, store = _ctx(args)
    from .apply.runner import make_drafter, run_application

    master, llm = _master(cfg), _llm(cfg)
    if args.mode:
        cfg["apply"]["mode"] = args.mode
    if store.funnel()["accepted"] and not args.force:
        sys.exit("You've accepted an offer, so applying is paused. Use --force to apply anyway.")
    limit = cfg["apply"]["daily_limit"] - store.applied_today()
    jobs = _target_jobs(store, args, ["tailored"])
    if limit <= 0 and not args.job:
        sys.exit(f"Daily limit of {cfg['apply']['daily_limit']} applications reached.")
    current = master.experience[0].company if master.experience and master.experience[0].end.lower() == "present" else ""
    for j in jobs[: max(limit, 1)]:
        if not j["resume_path"]:
            print(f"job {j['id']}: run `hireloop tailor --job {j['id']}` first")
            continue
        print(_fmt_job(j))
        res = run_application(dict(j), cfg, j["resume_path"], j["cover_letter_path"],
                              make_drafter(llm, master.to_prompt(), dict(j), cfg["profile"]),
                              current_company=current)
        if res.get("note"):
            print("  " + res["note"])
        if res["submitted"]:
            store.set_status(j["id"], "applied", source="hireloop")
            print("  ✓ submitted")
        else:
            ans = input("  Did you submit this application? [y/N] ").strip().lower()
            if ans == "y":
                store.set_status(j["id"], "applied", source="you")
        store.add_event(j["id"], "note", summary="apply attempt", data=res)


def cmd_mark(args):
    cfg, store = _ctx(args)
    ok = store.set_status(args.job, args.status, force=True, note=args.note or "", source="you")
    print("updated" if ok else "unchanged")


def cmd_sync_email(args):
    cfg, store = _ctx(args)
    from .inbox import sync_imap

    print(json.dumps(sync_imap(store, cfg, _llm(cfg, "classify"))))
    n = followup.generate_checkins(store, cfg)
    if n:
        print(f"{n} new question(s) for you — run `hireloop checkin`")


def cmd_checkin(args):
    cfg, store = _ctx(args)
    followup.generate_checkins(store, cfg)
    pending = store.pending_checkins()
    if not pending:
        print("Nothing to ask right now.")
        return
    for c in pending:
        choices = list(followup.ANSWERS[c["reason"]])
        print(f"\n{c['question']}")
        for i, ch in enumerate(choices, 1):
            print(f"  {i}) {ch}")
        raw = input("  answer (number, Enter to skip): ").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= len(choices):
            continue
        new = followup.apply_answer(store, c["id"], choices[int(raw) - 1])
        if new == "accepted":
            print("  🎉 Congratulations! Applying is now paused (`hireloop apply --force` overrides).\n"
                  "     Consider withdrawing other active applications: `hireloop mark <id> withdrawn`.")
        elif new:
            print(f"  status → {new}")


def cmd_status(args):
    cfg, store = _ctx(args)
    f = store.funnel()
    width = max(f.values() or [1]) or 1
    for s in STATUSES:
        if f[s]:
            print(f"{s:<12} {f[s]:>4} {'█' * max(1, int(30 * f[s] / width))}")
    applied = sum(f[s] for s in STATUSES[STATUSES.index("applied"):])
    if applied:
        replied = sum(f[s] for s in ("in_review", "assessment", "interview", "offer", "accepted", "declined", "rejected"))
        interviews = sum(f[s] for s in ("interview", "offer", "accepted", "declined"))
        print(f"\nresponse rate {100 * replied / applied:.0f}%   interview rate {100 * interviews / applied:.0f}%")
    p = store.pending_checkins()
    if p:
        print(f"\n{len(p)} question(s) waiting — run `hireloop checkin`")


def cmd_dashboard(args):
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(args.config), host=args.host, port=args.port)


def cmd_run(args):
    """Daily routine: discover → score → tailor top N → sync email → check-ins."""
    cfg, store = _ctx(args)
    from .discover import discover, score_jobs

    print("discover:", discover(store, cfg))
    print("score:", score_jobs(store, cfg, _master(cfg), _llm(cfg, "classify")))
    args.job = None
    cmd_tailor(args)
    if cfg["email"].get("imap_user"):
        try:
            cmd_sync_email(args)
        except SystemExit as e:
            print(f"email sync skipped: {e}")
    print("\nNext: `hireloop apply` to work through tailored jobs, `hireloop checkin` for questions.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hireloop", description=__doc__)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config.yaml and resume.yaml").set_defaults(fn=cmd_init)
    p = sub.add_parser("import-resume", help="convert your PDF/DOCX resume to resume.yaml (uses AI)")
    p.add_argument("file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_import_resume)
    sub.add_parser("discover", help="search job boards").set_defaults(fn=cmd_discover)
    p = sub.add_parser("score", help="score discovered jobs against your resume")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("add", help="add a job manually")
    p.add_argument("url")
    p.add_argument("--company", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--location")
    p.add_argument("--description-file")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("list", help="list jobs")
    p.add_argument("--status", help="comma separated, e.g. shortlisted,tailored")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show", help="details and timeline for one job")
    p.add_argument("job", type=int)
    p.set_defaults(fn=cmd_show)
    for name, fn, helptext in (("tailor", cmd_tailor, "tailor resume + cover letter"),
                               ("apply", cmd_apply, "fill application forms")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--job", type=int)
        p.add_argument("--top", type=int, default=5)
        if name == "apply":
            p.add_argument("--mode", choices=["review", "auto"])
            p.add_argument("--force", action="store_true", help="apply even after accepting an offer")
        p.set_defaults(fn=fn)
    p = sub.add_parser("mark", help="set a job's status yourself")
    p.add_argument("job", type=int)
    p.add_argument("status", choices=STATUSES)
    p.add_argument("--note")
    p.set_defaults(fn=cmd_mark)
    sub.add_parser("sync-email", help="read replies from your inbox").set_defaults(fn=cmd_sync_email)
    sub.add_parser("checkin", help="answer questions about your applications").set_defaults(fn=cmd_checkin)
    sub.add_parser("status", help="funnel summary").set_defaults(fn=cmd_status)
    p = sub.add_parser("dashboard", help="web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_dashboard)
    p = sub.add_parser("run", help="daily routine: discover, score, tailor, sync email")
    p.add_argument("--top", type=int, default=5)
    p.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    try:
        args.fn(args)
    except LLMUnavailable as e:
        sys.exit(f"AI step failed: {e}")


if __name__ == "__main__":
    main()
