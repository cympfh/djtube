"""Keep pytest from creating data/live-seen.json in the repo.

djtube.app builds a hub while it is imported. This runs first and points that
import at a temp directory. Each test then gets its own tmp_path. Both
directories are removed when the session ends.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import pytest

import djtube.live as live

_IMPORT_LIVE = Path(tempfile.mkdtemp(prefix="djtube-pytest-live-"))
live._DEFAULT_SEEN = _IMPORT_LIVE / "live-seen.json"
atexit.register(lambda: shutil.rmtree(_IMPORT_LIVE, ignore_errors=True))

_REPO_DATA = Path(__file__).resolve().parents[1] / "data"


def _repo_live_files() -> list[Path]:
    names = ("live-secret", "live-seen.json", "live-seen.json.bak")
    return [path for name in names if (path := _REPO_DATA / name).exists()]


@pytest.fixture(autouse=True)
def _live_seen_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_DEFAULT_SEEN", tmp_path / "live-seen.json")
    yield
    assert _repo_live_files() == []


@pytest.fixture(scope="session", autouse=True)
def _remove_import_live_dir():
    yield
    shutil.rmtree(_IMPORT_LIVE, ignore_errors=True)
