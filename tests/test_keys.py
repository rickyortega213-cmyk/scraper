"""Saved keys: persistence, precedence, and the setup wizard."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gmscrape import keys as K
from gmscrape.config import Settings, load_env


@pytest.fixture
def config_path(tmp_path, monkeypatch) -> Path:
    """An isolated saved-keys file, and a clean environment restored afterwards.

    The wizard writes straight into os.environ (that is its job), so a plain
    monkeypatch.delenv is not enough - snapshot and restore the whole thing.
    """
    snapshot = dict(os.environ)
    path = tmp_path / "cfg" / "config.env"
    monkeypatch.setenv("GMSCRAPE_CONFIG", str(path))
    for field in K.KEY_FIELDS:
        monkeypatch.delenv(field.env, raising=False)
    yield path
    os.environ.clear()
    os.environ.update(snapshot)


def test_save_merge_clear_and_permissions(config_path):
    K.save_keys({"MAILTESTER_KEY": "sub_abcdefghij", "SCRAPERAPI_KEY": "deadbeef"})
    assert K.read_saved_keys() == {"MAILTESTER_KEY": "sub_abcdefghij", "SCRAPERAPI_KEY": "deadbeef"}
    K.save_keys({"OPENWEBNINJA_KEY": "ak_1", "SCRAPERAPI_KEY": ""})     # add one, clear one
    assert K.read_saved_keys() == {"MAILTESTER_KEY": "sub_abcdefghij", "OPENWEBNINJA_KEY": "ak_1"}
    assert oct(config_path.stat().st_mode)[-3:] == "600"
    assert "sub_abcdefghij" in config_path.read_text()


def test_saved_keys_are_defaults_not_overrides(config_path, monkeypatch):
    K.save_keys({"MAILTESTER_KEY": "saved", "SERPAPI_KEY": "saved-serp"})
    monkeypatch.setenv("MAILTESTER_KEY", "from-environment")
    monkeypatch.chdir(config_path.parent)            # no project .env here
    load_env()
    assert os.environ["MAILTESTER_KEY"] == "from-environment"   # env wins
    assert os.environ["SERPAPI_KEY"] == "saved-serp"             # saved fills the gap
    settings = Settings.from_env()
    assert settings.serpapi_key == "saved-serp"
    assert K.key_status(settings).maps == "serpapi"


def test_project_dotenv_beats_saved_keys(config_path, tmp_path, monkeypatch):
    K.save_keys({"SERPAPI_KEY": "saved"})
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env").write_text("SERPAPI_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(project)
    load_env()
    assert os.environ["SERPAPI_KEY"] == "from-dotenv"


def test_wizard_keeps_replaces_and_clears(config_path, monkeypatch):
    K.save_keys({"MAILTESTER_KEY": "sub_old_value_x", "OPENWEBNINJA_KEY": "ak_old"})
    K.load_saved_keys_into_env()
    answers = iter({
        "MAILTESTER_KEY": "",             # Enter -> keep
        "OPENWEBNINJA_KEY": "-",          # clear
        "SCRAPERAPI_KEY": "cafebabe",     # set
    }.items())
    scripted = dict(answers)
    shown: list[str] = []

    def prompt(text: str) -> str:
        shown.append(text)
        for env, value in scripted.items():
            if K.field_by_env(env).label in text:
                return value
        return ""

    updates = K.run_setup(prompt=prompt, echo=lambda m: None)
    assert updates == {"OPENWEBNINJA_KEY": "", "SCRAPERAPI_KEY": "cafebabe"}
    assert K.read_saved_keys() == {"MAILTESTER_KEY": "sub_old_value_x", "SCRAPERAPI_KEY": "cafebabe"}
    # Secrets are shown masked in the prompt, never in full.
    mailtester_prompt = next(t for t in shown if "MailTester" in t)
    assert "sub_old_value_x" not in mailtester_prompt and "sub_…e_x" in mailtester_prompt
    assert os.environ["SCRAPERAPI_KEY"] == "cafebabe" and "OPENWEBNINJA_KEY" not in os.environ


def test_wizard_can_walk_one_group(config_path):
    asked: list[str] = []
    K.run_setup(prompt=lambda t: asked.append(t) or "", echo=lambda m: None, groups=("verify",))
    assert asked and all("key" in t.lower() for t in asked)
    assert not any("Supabase" in t or "ScraperAPI" in t for t in asked)


def test_status_reflects_what_is_configured(config_path):
    status = K.key_status(Settings.from_env())
    assert status.maps == "none" and not status.any_configured
    status = K.key_status(Settings.from_env(scraperapi_key="k", mailtester_key="m",
                                            openwebninja_key="a"))
    assert (status.maps, status.verify, status.search) == ("scraperapi", "mailtester", "openwebninja")
    assert status.any_configured


def test_cli_run_refuses_without_keys_when_not_interactive(config_path, monkeypatch, tmp_path):
    from gmscrape import cli

    monkeypatch.setattr(K, "interactive", lambda: False)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["run", "dentist in austin tx", "--db", str(tmp_path / "t.sqlite")])
    assert code == 2


def _no_network_run_args(tmp_path: Path) -> list[str]:
    """A run that touches no API: the file provider over an empty places file."""
    places = tmp_path / "places.csv"
    places.write_text("name,website\n", encoding="utf-8")
    return ["run", "dentist in austin tx", "--maps-provider", "file", "--places-file", str(places),
            "--verify-provider", "local", "--no-crawl", "--no-verify",
            "--db", str(tmp_path / "t.sqlite"), "-o", str(tmp_path / "out")]


def test_cli_run_offers_to_change_keys(config_path, monkeypatch, tmp_path):
    """At startup: shows keys, Enter continues, 'k' opens the wizard."""
    from gmscrape import cli

    K.save_keys({"SERPAPI_KEY": "old-serp"})
    monkeypatch.setattr(K, "interactive", lambda: True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda text="": "k")
    opened: list[bool] = []
    monkeypatch.setattr(K, "run_setup", lambda **kw: opened.append(True) or {})
    assert cli.main(_no_network_run_args(tmp_path)) == 0
    assert opened == [True]


def test_cli_run_can_be_cancelled_at_the_key_check(config_path, monkeypatch, tmp_path):
    from gmscrape import cli

    K.save_keys({"SERPAPI_KEY": "old-serp"})
    monkeypatch.setattr(K, "interactive", lambda: True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda text="": "q")
    assert cli.main(_no_network_run_args(tmp_path)) == 2


def test_yes_flag_skips_the_startup_prompt(config_path, monkeypatch, tmp_path):
    from gmscrape import cli

    K.save_keys({"SERPAPI_KEY": "old-serp"})
    monkeypatch.setattr(K, "interactive", lambda: True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda text="": pytest.fail("must not prompt with -y"))
    assert cli.main(_no_network_run_args(tmp_path) + ["-y"]) == 0


def test_first_run_opens_the_wizard_automatically(config_path, monkeypatch, tmp_path):
    """Nothing configured + a person at the keyboard -> setup runs, then the run proceeds."""
    from gmscrape import cli
    from gmscrape.core.pipeline import RunReport

    monkeypatch.setattr(K, "interactive", lambda: True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda text="": "")

    def fake_setup(**kwargs):
        K.save_keys({"SERPAPI_KEY": "just-entered"})
        os.environ["SERPAPI_KEY"] = "just-entered"
        return {"SERPAPI_KEY": "just-entered"}

    monkeypatch.setattr(K, "run_setup", fake_setup)

    seen: dict = {}

    class FakePipeline:
        def __init__(self, settings, **kwargs):
            seen["settings"] = settings
            self.maps = type("M", (), {"name": settings.maps_provider})()
            self.verifier = type("V", (), {"name": "local"})()
            self.web_search = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def run(self, queries, **kwargs):
            return RunReport(run_id="x", queries=list(queries))

    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    code = cli.main(["run", "dentist in austin tx", "--db", str(tmp_path / "t.sqlite"),
                     "-o", str(tmp_path / "out")])
    assert code == 0
    assert K.read_saved_keys() == {"SERPAPI_KEY": "just-entered"}
    assert seen["settings"].serpapi_key == "just-entered"      # the run used the new key


def test_pasted_values_are_cleaned_and_checked():
    assert K.clean_value("SUPABASE_URL", " https://abc.supabase.co/rest/v1/leads?x=1 ") == "https://abc.supabase.co"
    assert K.clean_value("SUPABASE_URL", "abc.supabase.co") == "https://abc.supabase.co"
    assert K.clean_value("MCP_MAPS_URL", '"https://mcp.scraper.tech/key/"') == "https://mcp.scraper.tech/key"
    assert K.clean_value("MAILTESTER_KEY", "  sub_x  ") == "sub_x"
    assert K.check_value("MAILTESTER_KEY", "sub_x") is None
    assert "sub_" in K.check_value("MAILTESTER_KEY", "abc")
    assert "ak_" in K.check_value("OPENWEBNINJA_KEY", "sub_x")
    assert "sbp_" in K.check_value("SUPABASE_KEY", "sbp_token")
    assert K.check_value("SUPABASE_KEY", "sb_secret_abc") is None
    assert K.looks_like_value("ak_testkey0000000000000000000000000000000000000000")
    assert not K.looks_like_value("yes") and not K.looks_like_value("n") and not K.looks_like_value("")


def test_cli_keys_set_saves_without_prompts(config_path, monkeypatch, tmp_path):
    from gmscrape import cli

    monkeypatch.chdir(tmp_path)
    code = cli.main(["keys", "set", "MAILTESTER_KEY=sub_abc123456",
                     "SUPABASE_URL=https://abc.supabase.co/rest/v1/whatever",
                     "OPENWEBNINJA_KEY=ak_x"])
    assert code == 0
    saved = K.read_saved_keys(config_path)
    assert saved["MAILTESTER_KEY"] == "sub_abc123456"
    assert saved["SUPABASE_URL"] == "https://abc.supabase.co"
    assert cli.main(["keys", "set", "OPENWEBNINJA_KEY="]) == 0          # NAME= clears
    assert "OPENWEBNINJA_KEY" not in K.read_saved_keys(config_path)
    assert cli.main(["keys", "set", "NOT_A_KEY=1"]) == 2
    assert cli.main(["keys", "set", "MAILTESTER_KEY"]) == 2
    assert cli.main(["keys"]) == 0
