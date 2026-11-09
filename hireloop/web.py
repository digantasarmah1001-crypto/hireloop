"""Local web dashboard: pipeline board, job timeline, and outcome questions."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import followup
from .config import load_config
from .db import STATUSES, Store

COLUMNS = [
    ("To review", ["shortlisted"]),
    ("Ready to apply", ["tailored"]),
    ("Applied", ["applied", "in_review"]),
    ("Interviewing", ["assessment", "interview"]),
    ("Offer", ["offer"]),
    ("Closed", ["accepted", "declined", "rejected", "ghosted", "withdrawn"]),
]


def create_app(config_path: str = "config.yaml") -> FastAPI:
    cfg = load_config(config_path)
    tpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    tpl.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
    app = FastAPI(title="hireloop")

    def store() -> Store:
        return Store(cfg.resolve(cfg["db_path"]))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        s = store()
        followup.generate_checkins(s, cfg)
        cols = [(name, s.jobs(sts, order="updated_at DESC")) for name, sts in COLUMNS]
        f = s.funnel()
        applied = sum(f[x] for x in STATUSES[STATUSES.index("applied"):])
        interviews = sum(f[x] for x in ("interview", "offer", "accepted", "declined"))
        return tpl.TemplateResponse(request, "index.html", {
            "columns": cols, "checkins": s.pending_checkins(), "answers": followup.ANSWERS,
            "funnel": f, "applied": applied, "interviews": interviews,
            "events": s.events(limit=15),
        })

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: int):
        s = store()
        j = s.get_job(job_id)
        if not j:
            raise HTTPException(404)
        return tpl.TemplateResponse(request, "job.html", {
            "j": j, "events": s.events(job_id), "statuses": STATUSES,
            "checkins": s.checkin_history(job_id),
        })

    @app.post("/job/{job_id}/status")
    def set_status(job_id: int, status: str = Form(...), note: str = Form("")):
        if status not in STATUSES:
            raise HTTPException(400)
        store().set_status(job_id, status, force=True, note=note, source="you")
        return RedirectResponse(f"/job/{job_id}", status_code=303)

    @app.post("/checkin/{checkin_id}")
    def answer(checkin_id: int, answer: str = Form(...)):
        followup.apply_answer(store(), checkin_id, answer)
        return RedirectResponse("/", status_code=303)

    @app.get("/job/{job_id}/resume")
    def resume(job_id: int):
        j = store().get_job(job_id)
        out = cfg.resolve(cfg["output_dir"]).resolve()
        if not j or not j["resume_path"]:
            raise HTTPException(404)
        p = Path(j["resume_path"]).resolve()
        if out not in p.parents or not p.exists():
            raise HTTPException(404)
        return FileResponse(p, filename=p.name)

    return app
