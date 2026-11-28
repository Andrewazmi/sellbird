"""Projects (docs/projects.md): finding them, their homes, and the optional
sellbird.projects.yaml."""

from pathlib import Path

import pytest

from sellbird import projects
from sellbird.config import ConfigError
from sellbird.projects import (
    UnknownProject, check_name, default_project, discover, enabled, install_root,
    live_settings, load_settings, project_home,
)
from sellbird.workspace_home import Home, use_home


def make_project(root: Path, name: str, yaml: str = "persona:\n  company: Example Co\n") -> Path:
    folder = root / "projects" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sellbird.yaml").write_text(yaml, encoding="utf-8")
    return folder


def test_no_projects_folder_means_no_projects(tmp_path):
    assert discover(tmp_path) == []
    assert not enabled(tmp_path)
    assert default_project(tmp_path) is None


def test_discover_needs_a_valid_name_and_a_config(tmp_path):
    make_project(tmp_path, "segment-b")
    make_project(tmp_path, "product-a")
    (tmp_path / "projects" / "no-config").mkdir()
    make_project(tmp_path, "Upper")             # not a valid name
    make_project(tmp_path, "-leading-dash")
    (tmp_path / "projects" / "stray.txt").write_text("x", encoding="utf-8")
    assert discover(tmp_path) == ["product-a", "segment-b"]
    assert enabled(tmp_path)


@pytest.mark.parametrize("name", ["a", "product-a", "x9", "a" * 40])
def test_good_names(name):
    assert check_name(name) == name


@pytest.mark.parametrize("name", ["", "A", "-a", "a_b", "a.b", "../a", "a/b", "a" * 41, None])
def test_bad_names(name):
    with pytest.raises(UnknownProject):
        check_name(name)


def test_for_project_shares_keys_mailboxes_and_login(tmp_path):
    h = Home.for_project(tmp_path, "product-a")
    folder = tmp_path / "projects" / "product-a"
    assert h.project == "product-a"
    assert h.shared_dir == tmp_path / "data"
    assert h.root == folder
    assert h.data_dir == folder / "data"
    assert h.db_path == folder / "data" / "sellbird.db"
    assert h.config_file == folder / "sellbird.yaml"
    assert h.prompts_dir == folder / "prompts"
    assert h.skills_dir == folder / "skills"
    # Shared with the root.
    assert h.env_file == tmp_path / ".env"
    assert h.m365_token_file == tmp_path / "data" / "m365_token.json"
    assert h.gmail_token_file == tmp_path / "data" / "gmail_token.json"
    assert h.dashboard_token_file == tmp_path / "data" / "dashboard-token"
    assert h.agent_token_file == tmp_path / "data" / "agent-token"
    assert not h.hosted


def test_for_project_refuses_a_path(tmp_path):
    with pytest.raises(UnknownProject):
        Home.for_project(tmp_path, "../elsewhere")


def test_root_home_is_not_a_project(tmp_path):
    h = Home.for_root(tmp_path)
    assert h.project is None and h.shared_dir is None


def test_project_home_only_for_existing_projects(tmp_path):
    make_project(tmp_path, "product-a")
    assert project_home(tmp_path, "product-a") == Home.for_project(tmp_path, "product-a")
    with pytest.raises(UnknownProject):
        project_home(tmp_path, "product-b")
    with pytest.raises(UnknownProject):
        project_home(tmp_path, "Product-A")


def test_install_root_from_a_project_or_the_root(tmp_path):
    with use_home(Home.for_project(tmp_path, "product-a")):
        assert install_root() == tmp_path
    with use_home(Home.for_root(tmp_path)):
        assert install_root() == tmp_path


def test_settings_default_when_missing(tmp_path):
    s = load_settings(tmp_path)
    assert s.default == "" and s.inbox_daily_cap == 0
    assert s.company_claim_days == 60 and s.do_not_contact == []


def test_settings_read_and_validated(tmp_path):
    (tmp_path / "sellbird.projects.yaml").write_text(
        "default: segment-b\ninbox_daily_cap: 40\ncompany_claim_days: 30\n"
        "do_not_contact:\n  - Competitor.Example\n  - someone@example.org\n",
        encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.default == "segment-b" and s.inbox_daily_cap == 40 and s.company_claim_days == 30
    assert s.do_not_contact == ["competitor.example", "someone@example.org"]


@pytest.mark.parametrize("text", ["inbox_daily_cap: -1\n", "- a\n- b\n", "company_claim_days: 0\n",
                                  "default: [unclosed\n"])
def test_bad_settings_raise(tmp_path, text):
    (tmp_path / "sellbird.projects.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_live_settings_keep_the_last_good_list(tmp_path):
    path = tmp_path / "sellbird.projects.yaml"
    path.write_text("do_not_contact: [competitor.example]\n", encoding="utf-8")
    projects._last_good.clear()
    assert live_settings(tmp_path).do_not_contact == ["competitor.example"]
    path.write_text("do_not_contact: [competitor.example\n  oops", encoding="utf-8")
    assert live_settings(tmp_path).do_not_contact == ["competitor.example"]


def test_default_project(tmp_path):
    make_project(tmp_path, "product-a")
    make_project(tmp_path, "segment-b")
    projects._last_good.clear()
    assert default_project(tmp_path) == "product-a"
    (tmp_path / "sellbird.projects.yaml").write_text("default: segment-b\n", encoding="utf-8")
    assert default_project(tmp_path) == "segment-b"
    (tmp_path / "sellbird.projects.yaml").write_text("default: gone\n", encoding="utf-8")
    assert default_project(tmp_path) == "product-a"


# ── Prompts, skills and email rules: the project's, then the root's, then shipped ──

def test_knowledge_resolves_per_file(tmp_path):
    from sellbird.brain import _read_knowledge
    from sellbird.home import bundled_defaults
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "writer.md").write_text("root writer", encoding="utf-8")
    own = tmp_path / "projects" / "product-a" / "prompts"
    with use_home(Home.for_project(tmp_path, "product-a")):
        assert _read_knowledge("prompts", "writer") == "root writer"
        own.mkdir(parents=True)
        (own / "writer.md").write_text("project writer", encoding="utf-8")
        assert _read_knowledge("prompts", "writer") == "project writer"
        shipped = (bundled_defaults() / "prompts" / "handler.md").read_text(encoding="utf-8")
        assert _read_knowledge("prompts", "handler") == shipped
        with pytest.raises(FileNotFoundError):
            _read_knowledge("prompts", "no-such-prompt")
    with use_home(Home.for_root(tmp_path)):
        assert _read_knowledge("prompts", "writer") == "root writer"


def test_new_project_reads_first_projects_trained_skill(tmp_path):
    """The review's case: before `projects init`, training wrote the root's
    skills/product_knowledge.md. A project must never sell what it says."""
    from sellbird.brain import _read_knowledge
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "product_knowledge.md").write_text("We sell Product A.", encoding="utf-8")
    (tmp_path / "skills" / "email_frameworks.md").write_text("Shared rules.", encoding="utf-8")
    (tmp_path / "projects" / "segment-b").mkdir(parents=True)
    with use_home(Home.for_project(tmp_path, "segment-b")):
        assert _read_knowledge("skills", "product_knowledge") == ""      # not trained yet
        assert _read_knowledge("skills", "email_frameworks") == "Shared rules."   # still shared
        own = tmp_path / "projects" / "segment-b" / "skills"
        own.mkdir()
        (own / "product_knowledge.md").write_text("We sell Product B.", encoding="utf-8")
        assert _read_knowledge("skills", "product_knowledge") == "We sell Product B."
    with use_home(Home.for_root(tmp_path)):
        assert _read_knowledge("skills", "product_knowledge") == "We sell Product A."


def test_email_rules_resolve_per_file(tmp_path):
    from types import SimpleNamespace
    from sellbird.config import load_email_rules
    config = SimpleNamespace(writer=SimpleNamespace(email_rules="skills/email_rules.md"))
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "email_rules.md").write_text("root rules", encoding="utf-8")
    with use_home(Home.for_project(tmp_path, "product-a")):
        assert load_email_rules(config) == "root rules"
        own = tmp_path / "projects" / "product-a" / "skills"
        own.mkdir(parents=True)
        (own / "email_rules.md").write_text("project rules", encoding="utf-8")
        assert load_email_rules(config) == "project rules"


# ── Mailbox tokens: several loops may refresh one file (review N4) ──

@pytest.mark.parametrize("module", ["gmail", "microsoft"])
def test_a_token_is_written_whole_and_private(tmp_path, monkeypatch, module):
    import importlib
    import os
    import sys
    mod = importlib.import_module(f"sellbird.integrations.{module}")
    with use_home(Home.for_root(tmp_path)):
        mod._save_token({"refresh_token": "first"})
        path = Path(getattr(Home.for_root(tmp_path),
                            "gmail_token_file" if module == "gmail" else "m365_token_file"))
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o600

        def broken(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(os, "replace", broken)
        with pytest.raises(OSError):
            mod._save_token({"refresh_token": "second"})
    assert '"first"' in path.read_text(encoding="utf-8")       # the old token, whole
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
