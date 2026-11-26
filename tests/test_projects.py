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
