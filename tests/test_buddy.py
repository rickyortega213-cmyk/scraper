"""`scraper buddy`: the guided flow for non-technical use."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gmscrape import buddy as B
from gmscrape import keys as K


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    snapshot = dict(os.environ)
    monkeypatch.setenv("GMSCRAPE_CONFIG", str(tmp_path / "cfg" / "config.env"))
    for key in B.BUDDY_KEYS:
        monkeypatch.delenv(key.env, raising=False)
    monkeypatch.delenv("GENERIC_MAPS_CONFIG", raising=False)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


class Script:
    """Answers prompts in order; records what was asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.said: list[str] = []

    def prompt(self, text: str) -> str:
        self.asked.append(text)
        return self.answers.pop(0) if self.answers else ""

    def echo(self, text: str) -> None:
        self.said.append(text)


def test_review_keys_keep_replace_add_remove(clean_env):
    K.save_keys({"MCP_MAPS_URL": "https://mcp.scraper.tech/a25e0000000000000000000000000ce8",
                 "MAILTESTER_KEY": "sub_old_key_1"})
    K.load_saved_keys_into_env()
    script = Script(
        "",                     # Scraper Tech: keep (Enter = yes)
        "n", "sub_new_key_2",   # MailTester: replace
        "y", "ak_new",          # OpenWeb Ninja: not set -> add
        "",                     # Supabase URL: optional -> skip
        "", "",                 # Supabase key, token: skip
    )
    updates = B.review_keys(script.prompt, script.echo)
    assert updates == {"MAILTESTER_KEY": "sub_new_key_2", "OPENWEBNINJA_KEY": "ak_new"}
    assert os.environ["MAILTESTER_KEY"] == "sub_new_key_2"
    assert K.read_saved_keys()["MCP_MAPS_URL"].endswith("ce8")          # kept
    # keys are shown masked, never in full
    assert not any("sub_old_key_1" in t for t in script.asked)
    assert any("sub_…y_1" in t and "keep it? [Y/n]" in t for t in script.asked)
    assert any("not set" in t and "add it now?" in t for t in script.asked)


def test_review_keys_can_remove_one(clean_env):
    K.save_keys({"OPENWEBNINJA_KEY": "ak_old"})
    K.load_saved_keys_into_env()
    script = Script("n", "n", "n", "-", "", "", "")   # decline the two missing required keys, then remove OpenWeb Ninja
    updates = B.review_keys(script.prompt, script.echo)
    assert updates == {"OPENWEBNINJA_KEY": ""}
    assert "OPENWEBNINJA_KEY" not in K.read_saved_keys() and "OPENWEBNINJA_KEY" not in os.environ


def test_collect_queries_by_pasting():
    script = Script("dentist in austin tx", "plumber in miami fl", "", "ignored")
    assert B.collect_queries(script.prompt, script.echo) == [
        "dentist in austin tx", "plumber in miami fl",
    ]


def test_collect_queries_from_txt_and_csv(tmp_path: Path):
    txt = tmp_path / "searches.txt"
    txt.write_text("# my list\ndentist in austin tx\n\nplumber in miami fl\n", encoding="utf-8")
    script = Script(str(txt))
    assert B.collect_queries(script.prompt, script.echo) == [
        "dentist in austin tx", "plumber in miami fl",
    ]

    with_header = tmp_path / "searches.csv"
    with_header.write_text("query,notes\ndentist in austin tx,hot\nroofer in denver co,\n", encoding="utf-8")
    script = Script(f'"{with_header}"')                 # drag-and-drop quotes are fine
    assert B.collect_queries(script.prompt, script.echo) == [
        "dentist in austin tx", "roofer in denver co",
    ]

    headerless = tmp_path / "plain.csv"
    headerless.write_text("med spa in scottsdale az\nlaw firm in charlotte nc\n", encoding="utf-8")
    script = Script(str(headerless))
    assert B.collect_queries(script.prompt, script.echo) == [
        "med spa in scottsdale az", "law firm in charlotte nc",
    ]


def test_buddy_runs_the_pipeline_with_the_answers(clean_env, monkeypatch, tmp_path):
    K.save_keys({"MCP_MAPS_URL": "https://mcp.example/key", "MAILTESTER_KEY": "sub_x"})
    captured: dict = {}

    def fake_run(args):
        captured["queries"] = list(args.queries)
        captured["limit"] = args.results_per_query
        captured["confirm"] = args.confirm_keys_on_start
        return 0

    monkeypatch.setattr("gmscrape.cli.cmd_run", fake_run)
    monkeypatch.setattr("gmscrape.banner.print_banner", lambda console=None: None)
    script = Script(
        "", "", "", "", "", "",                  # keep / skip every key
        "dentist in austin tx", "plumber in miami fl", "",   # searches
        "25",                                    # businesses per search
        "",                                      # Start? -> yes
    )
    assert B.buddy(script.prompt, script.echo) == 0
    assert captured == {
        "queries": ["dentist in austin tx", "plumber in miami fl"],
        "limit": 25,
        "confirm": False,                        # keys were just reviewed - no second prompt
    }


def test_buddy_explains_when_maps_is_not_set_up(clean_env, monkeypatch):
    monkeypatch.setattr("gmscrape.banner.print_banner", lambda console=None: None)
    script = Script("n", "n", "", "", "", "")               # declines every key
    assert B.buddy(script.prompt, script.echo) == 2
    assert any("mcp.scraper.tech" in line for line in script.said)


def test_scraper_command_routes(monkeypatch):
    calls: list = []
    monkeypatch.setattr(B, "buddy", lambda **kw: calls.append("buddy") or 0)
    monkeypatch.setattr("gmscrape.cli.main", lambda argv: calls.append(("cli", argv)) or 0)
    assert B.main([]) == 0 and B.main(["buddy"]) == 0
    assert B.main(["keys"]) == 0
    assert calls == ["buddy", "buddy", ("cli", ["keys"])]
