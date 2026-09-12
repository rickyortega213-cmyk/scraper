"""A run that gets killed picks itself back up; memory is watched, not hoped for."""

from __future__ import annotations

import argparse
import os

import pytest

from gmscrape import cli


def _args(**extra):
    ns = argparse.Namespace(_argv=["run", "-y", "plumber in austin tx"], env_file=None,
                            db_path="/tmp/x.sqlite", log_level=None, out_dir="out", basename="leads",
                            no_supervise=False)
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def test_supervisor_resumes_after_a_kill_and_stops_on_a_clean_exit(monkeypatch):
    monkeypatch.setattr(cli, "echo", lambda *a, **k: None)
    seen: list[list[str]] = []
    codes = iter([-9, 137, 1, 0])

    def fake_child(argv):
        seen.append(list(argv))
        return next(codes)

    code = cli.supervise(_args(), run_child=fake_child, pause=0)
    assert code == 0
    assert seen[0] == ["run", "-y", "plumber in austin tx"]
    assert seen[1][:2] == ["resume", "-y"] and "--db" in seen[1] and "-o" in seen[1]
    assert len(seen) == 4                                      # three restarts, then done


def test_supervisor_leaves_ctrl_c_and_refusals_alone(monkeypatch):
    monkeypatch.setattr(cli, "echo", lambda *a, **k: None)
    for clean in (130, 2):
        calls = []
        assert cli.supervise(_args(), run_child=lambda argv: calls.append(argv) or clean, pause=0) == clean
        assert len(calls) == 1


def test_supervisor_gives_up_eventually(monkeypatch):
    monkeypatch.setattr(cli, "echo", lambda *a, **k: None)
    monkeypatch.setattr(cli, "SUPERVISE_MAX_RESTARTS", 3)
    calls = []
    assert cli.supervise(_args(), run_child=lambda argv: calls.append(argv) or -9, pause=0) == -9
    assert len(calls) == 4


def test_only_a_terminal_session_is_supervised(monkeypatch):
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    monkeypatch.delenv("GMSCRAPE_CHILD", raising=False)
    assert cli._supervised(_args())
    assert not cli._supervised(_args(no_supervise=True))
    monkeypatch.setenv("GMSCRAPE_CHILD", "1")
    assert not cli._supervised(_args())                         # the child never supervises itself
    monkeypatch.delenv("GMSCRAPE_CHILD", raising=False)
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    assert not cli._supervised(_args())                         # tests and scripts run inline


def test_memory_is_measured_and_bounded(settings, site_server):
    from gmscrape.core.pipeline import Pipeline
    from gmscrape.util import current_rss_mb, total_ram_mb
    from gmscrape.web.extract import _PARSE_GATE

    assert current_rss_mb() > 10 and total_ram_mb() > 100
    assert _PARSE_GATE._initial_value == 4
    with Pipeline(settings) as pipeline:
        limit = pipeline.memory_limit_mb()
        assert 1024 <= limit <= 6144
        settings.memory_limit_mb = 2500
        assert pipeline.memory_limit_mb() == 2500
