"""Config loading. Single YAML file; every section has defaults."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "profile": {"answers": {}},
    "resume_path": "resume.yaml",
    "search": {
        "sites": ["indeed", "linkedin", "google"],
        "terms": [],
        "locations": [""],
        "country_indeed": "usa",
        "hours_old": 72,
        "results_per_search": 30,
        "exclude_companies": [],
        "exclude_title_words": [],
    },
    "matching": {"min_fit_score": 60, "min_keyword_score": 40, "use_llm_scoring": True},
    "apply": {"mode": "review", "daily_limit": 15, "headless": False},
    "email": {
        "imap_host": "imap.gmail.com",
        "imap_user": "",
        "password_env": "HIRELOOP_IMAP_PASSWORD",
        "folder": "INBOX",
        "lookback_days": 30,
    },
    "followup": {"ask_after_days_no_reply": 21, "ask_after_interview_days": 7},
    "llm": {"model": "claude-opus-5-5", "effort": "medium", "classify_effort": "low"},
    "db_path": "data/hireloop.db",
    "output_dir": "output",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    """dict with attribute access for top-level sections."""

    path: Path

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e

    def resolve(self, rel: str) -> Path:
        """Resolve a path relative to the config file's directory."""
        p = Path(rel)
        return p if p.is_absolute() else (self.path.parent / p)


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    cfg = Config(_merge(DEFAULTS, data))
    cfg.path = path.resolve()
    return cfg
