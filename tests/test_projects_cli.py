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
    (root / "sellbird.example-extension.yaml").write_text("enabled: true\n", encoding="utf-8")
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")   # not Sellbird's
    (root / "skills").mkdir()
    (root / "skills" / "product_knowledge.md").write_text("We sell roofing.", encoding="utf-8")
    (root / "skills" / "email_frameworks.md").write_text("Shared rules.", encoding="utf-8")
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
    assert ("sellbird.example-extension.yaml  ->  "
            "projects/product-a/sellbird.example-extension.yaml") in out
    assert "skills/product_knowledge.md  ->  projects/product-a/skills/product_knowledge.md" in out
    assert "compose.yaml" not in out.replace("sellbird.example-extension.yaml", "")
    assert "data/sellbird.db  ->  projects/product-a/data/sellbird.db" in out
    assert "Nothing changed" in out


def test_init_apply_makes_the_first_project(tmp_path):
    asyncio.run(single_install(tmp_path))
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 0
    folder = tmp_path / "projects" / "product-a"

    # Moved.
    assert (folder / "sellbird.yaml").read_text(encoding="utf-8") == trained("Example Roofing")
    assert (folder / "sellbird.example-extension.yaml").exists()
    assert (folder / "skills" / "product_knowledge.md").read_text(encoding="utf-8") == "We sell roofing."
    assert (folder / "data" / "sellbird.db").exists()
    assert not (tmp_path / "sellbird.local.yaml").exists()
    assert not (tmp_path / "sellbird.example-extension.yaml").exists()
    assert not (tmp_path / "skills" / "product_knowledge.md").exists()
    assert (tmp_path / "compose.yaml").exists()
    assert (tmp_path / "skills" / "email_frameworks.md").exists()      # shared
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
    assert {"sellbird.local.yaml", "sellbird.example-extension.yaml", "data/sellbird.db",
            "skills/product_knowledge.md"} <= names

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


def test_init_refuses_while_the_dashboard_is_open(tmp_path, capsys):
    import os
    from sellbird import dashboard_instance
    asyncio.run(single_install(tmp_path))
    dashboard_instance.write(tmp_path / "data", host="127.0.0.1", port=5555, instance="x")
    before = tree(tmp_path)
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 1
    assert "dashboard is open" in capsys.readouterr().out
    assert tree(tmp_path) == before
    # A file left by a dashboard that's gone doesn't block.
    dashboard_instance.write(tmp_path / "data", host="127.0.0.1", port=5555, instance="x")
    raw = (tmp_path / "data" / "dashboard.json").read_text(encoding="utf-8")
    (tmp_path / "data" / "dashboard.json").write_text(
        raw.replace(f'"pid": {os.getpid()}', '"pid": 999999'), encoding="utf-8")
    assert projects_cli._busy(tmp_path) == ""


def test_init_puts_every_file_back_when_a_step_fails(tmp_path, monkeypatch, capsys):
    asyncio.run(single_install(tmp_path))
    before = tree(tmp_path)

    async def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(projects_cli, "_seed_shared", broken)
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 1
    assert "every file is back" in capsys.readouterr().out
    after = tree(tmp_path)
    [backup] = [k for k in after if k.startswith("data/backup-")]
    after.pop(backup)
    assert after == before
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "data" / "shared.db").exists()
    assert not (tmp_path / "sellbird.projects.yaml").exists()
    assert projects.discover(tmp_path) == []


def test_init_backs_up_a_symlinked_config_as_its_contents(tmp_path):
    asyncio.run(single_install(tmp_path))
    real = tmp_path / "elsewhere.txt"
    (tmp_path / "sellbird.local.yaml").rename(real)
    (tmp_path / "sellbird.local.yaml").symlink_to(real)
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 0
    [backup] = (tmp_path / "data").glob("backup-*.tar.gz")
    with tarfile.open(backup) as tar:
        member = tar.getmember("sellbird.local.yaml")
        assert member.isfile()
        assert b"Example Roofing" in tar.extractfile(member).read()


def test_init_carries_the_agent_switch_and_reseed_adds_whats_missing(tmp_path, capsys):
    from sellbird.dashboard import AGENT_ACCESS_KEY
    asyncio.run(single_install(tmp_path))

    async def switch_off():
        await StateManager(str(tmp_path / "data" / "sellbird.db")).set_setting(AGENT_ACCESS_KEY, "off")
    asyncio.run(switch_off())
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 0
    shared = SharedState(tmp_path / "data")
    assert asyncio.run(shared.get_setting(AGENT_ACCESS_KEY)) == "off"

    # shared.db lost (a restore, say): --reseed brings it back, and twice is the same as once.
    for f in tmp_path.joinpath("data").glob("shared.db*"):
        f.unlink()
    assert projects_cli.cmd_reseed(tmp_path) == 0
    assert projects_cli.cmd_reseed(tmp_path) == 0

    async def check():
        claims = await shared.list_claims()
        assert [(c["domain"], c["project"]) for c in claims] == [("acme.example", "product-a")]
        assert await shared.get_setting(AGENT_ACCESS_KEY) == "off"
        with use_home(projects.project_home(tmp_path, "product-a")):
            assert len(await StateManager().list_suppressions()) == 1
    asyncio.run(check())
    assert "product-a: 0 suppression(s) added" in capsys.readouterr().out


def test_init_makes_the_first_project_the_default(tmp_path):
    """A project added later, empty and first in the alphabet, must not be
    where the dashboard opens."""
    asyncio.run(single_install(tmp_path))
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 0
    assert projects_cli.cmd_new(tmp_path, "an-empty-one") == 0
    assert projects.default_project(tmp_path) == "product-a"


def test_init_keeps_a_default_already_set(tmp_path):
    asyncio.run(single_install(tmp_path))
    settings = tmp_path / "sellbird.projects.yaml"
    settings.write_text("# mine\ndefault: segment-b\ninbox_daily_cap: 40\n", encoding="utf-8")
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 0
    assert settings.read_text(encoding="utf-8") == "# mine\ndefault: segment-b\ninbox_daily_cap: 40\n"


def test_init_adds_the_default_beside_other_settings(tmp_path):
    asyncio.run(single_install(tmp_path))
    settings = tmp_path / "sellbird.projects.yaml"
    settings.write_text("inbox_daily_cap: 40", encoding="utf-8")
    assert projects_cli.cmd_init(tmp_path, "product-a", apply=True) == 0
    assert projects.load_settings(tmp_path).default == "product-a"
    assert projects.load_settings(tmp_path).inbox_daily_cap == 40


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
    assert len(problems) == 1 and problems[0].startswith("Project segment-b:")
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


# ── Every command in projects mode: its project, or the whole install (review B4) ──


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    from sellbird import home, paths
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(home, "seed_home", lambda *a, **k: None)
    monkeypatch.delenv("SELLBIRD_PROJECT", raising=False)
    for name, company in (("product-a", "Example Roofing"), ("segment-b", "Example Dental")):
        h = make_project(tmp_path, name, company)
        asyncio.run(StateManager(str(h.db_path)).init_db())
    return tmp_path


def _sellbird(monkeypatch, *argv):
    import sys
    from sellbird import cli
    monkeypatch.setattr(sys, "argv", ["sellbird", *argv])
    cli.main()


def _setting(root, name, key):
    db = root / "projects" / name / "data" / "sellbird.db"
    return asyncio.run(StateManager(str(db)).get_setting(key))


def test_sending_pause_without_a_project_pauses_every_project(two_projects, monkeypatch):
    _sellbird(monkeypatch, "sending", "pause")
    assert _setting(two_projects, "product-a", "sending_paused")
    assert _setting(two_projects, "segment-b", "sending_paused")
    _sellbird(monkeypatch, "--project", "segment-b", "sending", "resume")
    assert _setting(two_projects, "product-a", "sending_paused")
    assert not _setting(two_projects, "segment-b", "sending_paused")
    assert not (two_projects / "data" / "sellbird.db").exists()


def test_a_suppression_from_the_cli_goes_to_the_shared_list(two_projects, monkeypatch):
    _sellbird(monkeypatch, "suppressions", "add", "gone@other.example")
    for name in ("product-a", "segment-b"):
        with use_home(projects.project_home(two_projects, name)):
            assert asyncio.run(StateManager().is_suppressed("gone@other.example"))
    assert not (two_projects / "data" / "sellbird.db").exists()


def test_a_project_command_without_a_project_is_refused(two_projects, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _sellbird(monkeypatch, "outbox")
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "product-a, segment-b" in out and "--project NAME outbox" in out
    assert not (two_projects / "data" / "sellbird.db").exists()


def test_a_project_comes_from_the_flag_or_the_environment(two_projects, monkeypatch):
    seen = []
    import sellbird.cli as cli
    monkeypatch.setattr(cli, "cmd_outbox", lambda args: seen.append(StateManager().db_path))
    _sellbird(monkeypatch, "--project", "segment-b", "outbox")
    monkeypatch.setenv("SELLBIRD_PROJECT", "product-a")
    _sellbird(monkeypatch, "outbox")
    assert seen == [str(two_projects / "projects" / n / "data" / "sellbird.db")
                    for n in ("segment-b", "product-a")]
    with pytest.raises(SystemExit) as exc:
        _sellbird(monkeypatch, "--project", "nope", "outbox")
    assert exc.value.code == 1


def test_a_project_flag_without_projects_is_refused(tmp_path, monkeypatch, capsys):
    from sellbird import home, paths
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(home, "seed_home", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        _sellbird(monkeypatch, "--project", "product-a", "outbox")
    assert exc.value.code == 2 and "no projects" in capsys.readouterr().out


def test_check_refuses_instantly_and_warns_about_a_shared_inbox_with_no_cap(tmp_path):
    make_project(tmp_path, "product-a", "Example Roofing")
    make_project(tmp_path, "segment-b", "Example Dental")       # same template address
    problems, warnings = projects_cli.check(tmp_path)
    assert problems == []
    assert any("inbox_daily_cap is 0" in w and "product-a, segment-b" in w for w in warnings)
    (tmp_path / "sellbird.projects.yaml").write_text("inbox_daily_cap: 30\n", encoding="utf-8")
    assert not any("inbox_daily_cap" in w for w in projects_cli.check(tmp_path)[1])
    legacy = make_project(tmp_path, "legacy-c", "Example Legacy")
    legacy.config_file.write_text(trained("Example Legacy").replace(
        'provider: "gmail"', 'provider: "instantly"', 1), encoding="utf-8")
    problems, _ = projects_cli.check(tmp_path)
    assert len(problems) == 1 and "legacy-c sends through Instantly" in problems[0]
