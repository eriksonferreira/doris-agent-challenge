"""Test helpers shared by proxy/tests and eval/tests. Not used by the running app."""
from dataclasses import replace
from pathlib import Path

from app.settings import Settings


def make_settings(tmp_path: Path, **over) -> Settings:
    return replace(Settings(), db_path=str(tmp_path / "proxy.db"), latency_scale=0.0, **over)
