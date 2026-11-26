"""`sellbird projects`: new, check, release, and init turning a single
install into its first project (a dry run first, then --apply)."""

import asyncio
import tarfile
from pathlib import Path

import pytest

from sellbird import pidfile, projects, projects_cli
from sellbird.config import load_config
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.shared import SharedState
from sellbird.state import StateManager
from sellbird.workspace_home import use_home
from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign
from tests.test_projects_run import TEMPLATE, make_project, trained


@pytest.fixture(autouse=True)
def _fresh_settings():
    projects._last_good.clear()
    yield
    projects._last_good.clear()


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


async def single_install(root: Path) -> None:
    """A trained install that has emailed one company and has one opt-out."""
    (root / ".env").write_text("SMTP_HOST=smtp.example.com\n", encoding="utf-8")
    (root / "sellbird.yaml").write_text(TEMPLATE, encoding="utf-8")            # the template
    (root / "sellbird.local.yaml").write_text(trained("Example Roofing"), encoding="utf-8")
    (root / "example-extension.yaml").write_text("enabled: true\n", encoding="utf-8")
    data = root / "data"
    data.mkdir()
    for name in ("gmail_token.json", "dashboard-token", "agent-token"):
        (data / name).write_text("x", encoding="utf-8")
    state = StateManager(str(data / "sellbird.db"))
    await state.init_db()
    company_id = await state.add_company(Company(name="Acme", domain="acme.example"))
    pid = await state.add_prospect(Prospect(
        first_name="Pat", last_name="Doe", title="Owner", company="Acme", company_id=company_id,
        email="pat@acme.example", email_status="verified", email_verified=True, status="new"))
    await seed_campaign(state, [pid])
    await make_sender(state, FakeProvider(), require_approval=False)._run_native()
    await state.add_suppression("address", "gone@other.example", "opted_out")


def test_init_dry_run_changes_nothing(tmp_path, capsys):
    asyncio.run(single_install(tmp_path))
    before = tree(tmp_path)
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=False) == 0
    assert tree(tmp_path) == before
    out = capsys.readouterr().out
    assert "sellbird.local.yaml  ->  projects/product-a/sellbird.yaml" in out
    assert "example-extension.yaml  ->  projects/product-a/example-extension.yaml" in out
    assert "data/sellbird.db  ->  projects/product-a/data/sellbird.db" in out
    assert "Nothing changed" in out


def test_init_apply_makes_the_first_project(tmp_path):
    asyncio.run(single_install(tmp_path))
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 0
    folder = tmp_path / "projects" / "product-a"

    # Moved.
    assert (folder / "sellbird.yaml").read_text(encoding="utf-8") == trained("Example Roofing")
    assert (folder / "example-extension.yaml").exists()
    assert (folder / "data" / "sellbird.db").exists()
    assert not (tmp_path / "sellbird.local.yaml").exists()
    assert not (tmp_path / "example-extension.yaml").exists()
    assert not (tmp_path / "data" / "sellbird.db").exists()
    # Kept at the root.
    assert (tmp_path / "sellbird.yaml").read_text(encoding="utf-8") == TEMPLATE
    assert (tmp_path / ".env").exists()
    for name in ("gmail_token.json", "dashboard-token", "agent-token"):
        assert (tmp_path / "data" / name).exists()
    # The backup holds everything moved.
    [backup] = (tmp_path / "data").glob("backup-*.tar.gz")
    with tarfile.open(backup) as tar:
        names = set(tar.getnames())
    assert {"sellbird.local.yaml", "example-extension.yaml", "data/sellbird.db"} <= names

    # A working home, with the shared layer seeded.
    assert projects.discover(tmp_path) == ["product-a"]

    async def check():
        with use_home(projects.project_home(tmp_path, "product-a")):
            assert load_config().persona.company == "Example Roofing"
            state = StateManager()
            assert (await state.get_prospect_by_email("pat@acme.example")).status == "contacted"
            assert await state.is_suppressed("gone@other.example") == "opted_out"
            assert len(await state.list_suppressions()) == 1      # copied, not doubled
        claims = await SharedState(tmp_path / "data").list_claims()
        assert [(c["domain"], c["project"]) for c in claims] == [("acme.example", "product-a")]
    asyncio.run(check())


def test_init_refuses_while_the_loop_runs(tmp_path, capsys):
    asyncio.run(single_install(tmp_path))
    before = tree(tmp_path)
    held = pidfile.hold_loop_lock(tmp_path / "data" / "sellbird.lock")
    try:
        assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 1
    finally:
        pidfile.release_loop_lock(held)
    assert "Stop it first" in capsys.readouterr().out
    after = tree(tmp_path)
    after.pop("data/sellbird.lock", None)
    assert after == before


@pytest.mark.parametrize("why", ["untrained", "has_projects", "bad_name"])
def test_init_refuses(tmp_path, why, capsys):
    (tmp_path / "sellbird.yaml").write_text(
        TEMPLATE if why == "untrained" else trained("Example Roofing"), encoding="utf-8")
    if why == "has_projects":
        make_project(tmp_path, "segment-b", "Example Dental")
    name = "Bad_Name" if why == "bad_name" else "product-a"
    assert projects_cli.cmd_init(tmp_path, name, apply=True) == 1
    assert not (tmp_path / "projects" / "product-a").exists()


def test_new_copies_the_untrained_template(tmp_path):
    assert projects_cli.cmd_new(tmp_path, "segment-b") == 0
    config = tmp_path / "projects" / "segment-b" / "sellbird.yaml"
    assert projects_cli._company(config) == "Your Company"
    assert projects.discover(tmp_path) == ["segment-b"]
    assert projects_cli.cmd_new(tmp_path, "segment-b") == 1   # never overwrites


def test_check_reports_bad_configs_and_the_shared_quota(tmp_path):
    make_project(tmp_path, "product-a", "Example Roofing")
    make_project(tmp_path, "segment-b", "Example Dental")
    problems, warnings = projects_cli.check(tmp_path)
    assert problems == []
    assert any("add up to 160%" in w for w in warnings)       # 80 + 80
    (tmp_path / "projects" / "segment-b" / "sellbird.yaml").write_text("persona: 3\n", encoding="utf-8")
    (tmp_path / "sellbird.projects.yaml").write_text("default: nope\n", encoding="utf-8")
    problems, warnings = projects_cli.check(tmp_path)
    assert len(problems) == 1 and problems[0].startswith("segment-b:")
    assert any("default: 'nope'" in w for w in warnings)


def test_release_ends_a_claim(tmp_path, capsys):
    shared = SharedState(tmp_path / "data")
    asyncio.run(shared.claim("acme.example", "product-a", 60))
    assert projects_cli.cmd_release(tmp_path, "https://www.Acme.example/") == 0
    assert asyncio.run(shared.list_claims()) == []
    assert projects_cli.cmd_release(tmp_path, "acme.example") == 1


def test_setup_and_train_take_a_project(tmp_path, monkeypatch, capsys):
    import sys
    from sellbird import cli, paths, setup
    from sellbird.workspace_home import current_home
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    make_project(tmp_path, "segment-b", "Example Dental")
    seen = []

    async def fake_setup():
        seen.append((current_home().project, setup._config_file()))

    monkeypatch.setattr(setup, "run_setup", fake_setup)
    monkeypatch.setattr(sys, "argv", ["sellbird", "setup", "--project", "segment-b"])
    cli.main()
    assert seen == [("segment-b", tmp_path / "projects" / "segment-b" / "sellbird.yaml")]

    monkeypatch.setattr(sys, "argv", ["sellbird", "setup", "--project", "nope"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1 and "nope" in capsys.readouterr().out


def test_training_a_project_writes_its_own_skills(tmp_path):
    from sellbird.trainer import _skills_dir
    from sellbird.workspace_home import Home
    with use_home(Home.for_project(tmp_path, "segment-b")):
        assert _skills_dir() == tmp_path / "projects" / "segment-b" / "skills"
        assert _skills_dir().is_dir()
