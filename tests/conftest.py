import shutil
from pathlib import Path

import pytest

from hireloop.config import load_config
from hireloop.db import Store
from hireloop.resume import Resume

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def master() -> Resume:
    return Resume.load(ROOT / "resume.example.yaml")


@pytest.fixture
def cfg(tmp_path):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    return load_config(tmp_path / "config.yaml")


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "t.db")


@pytest.fixture
def job_desc() -> str:
    return (ROOT / "tests" / "fixtures" / "job_backend.txt").read_text()
