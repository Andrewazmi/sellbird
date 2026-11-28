"""Projects in one process (sellbird.main): two heartbeats side by side, each
seeing only its own config, database and drafts; the homes held across tasks
and threads; the locks; and an install with no projects/ folder unchanged."""

import asyncio
from pathlib import Path

import pytest

import sellbird.main as main_mod
from sellbird import pidfile
from sellbird.config import EnvConfig, load_config
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.state import StateManager
from sellbird.workspace_home import Home, current_home, use_home

TEMPLATE = (Path(__file__).resolve().parent.parent / "sellbird.yaml").read_text(encoding="utf-8")


def trained(company: str) -> str:
    return TEMPLATE.replace('company: "Your Company"', f'company: "{company}"', 1)


def make_project(root: Path, name: str, company: str) -> Home:
    home = Home.for_project(root, name)
    home.root.mkdir(parents=True, exist_ok=True)
    home.config_file.write_text(trained(company), encoding="utf-8")
    return home


# ── ContextVars across tasks and threads ──

@pytest.mark.asyncio
async def test_each_task_and_its_threads_keep_their_project(tmp_path):
    seen = {}

    async def work(tag):
        await asyncio.sleep(0)
        in_thread = await asyncio.to_thread(lambda: current_home().project)
        await asyncio.sleep(0)
        seen[tag] = (current_home().project, in_thread, Path(StateManager().db_path))

    tasks = []
    for name in ("product-a", "segment-b"):
        with use_home(Home.for_project(tmp_path, name)):
            tasks.append(asyncio.create_task(work(name)))
    assert current_home().project is None          # nothing leaked out here
    await asyncio.gather(*tasks)
    for name in ("product-a", "segment-b"):
        project, in_thread, db = seen[name]
        assert project == name and in_thread == name
        assert db == tmp_path / "projects" / name / "data" / "sellbird.db"


# ── Two heartbeats in one process ──

class FakeBrain:
    provider_label = "Fake"
    model = ""
    paused_reason = ""

    def __init__(self, state, brain_config=None, transport=None):
        self.state = state

    async def preflight(self):
        return ""

    async def is_within_budget(self, *a, **k):
        return True


def _fake_agents(monkeypatch, record, both_started):
    import sellbird.agents.analyst as analyst_mod
    import sellbird.agents.handler as handler_mod
    import sellbird.agents.scout as scout_mod
    import sellbird.agents.sender as sender_mod
    import sellbird.agents.writer as writer_mod

    class Agent:
        is_native = False
        cold_only = False

        def __init__(self, brain, state, config, env):
            self.state, self.config = state, config

        async def run(self):
            return True

    class Writer(Agent):
        async def run(self):
            """Draft a campaign named after the config this project loaded."""
            home = current_home().project
            both_started.append(home)
            while len(set(both_started)) < 2:      # really side by side
                await asyncio.sleep(0.01)
            company = load_config().persona.company
            in_thread = await asyncio.to_thread(lambda: (current_home().project,
                                                         load_config().persona.company))
            await self.state.add_campaign(Campaign(
                id="", name=f"draft for {company}", channel="email",
                sequence=[EmailStep(step=1, subject="s", body="b")], prospect_ids=[],
                status="draft"))
            record[home] = {"config": self.config.persona.company, "loaded": company,
                            "thread": in_thread, "db": Path(self.state.db_path)}

    monkeypatch.setattr(scout_mod, "Scout", Agent)
    monkeypatch.setattr(writer_mod, "Writer", Writer)
    monkeypatch.setattr(sender_mod, "Sender", Agent)
    monkeypatch.setattr(handler_mod, "Handler", Agent)
    monkeypatch.setattr(analyst_mod, "Analyst", lambda state: Agent(None, state, None, None))


@pytest.fixture
def quiet_loop(monkeypatch):
    """The heartbeat with a fake brain and fake agents: one cycle, no network."""
    record, both_started = {}, []
    monkeypatch.setattr(main_mod, "Brain", FakeBrain)
    monkeypatch.setattr(main_mod, "load_env", lambda: EnvConfig())
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda config: False)

    async def write(*a, **k):
        return "write_campaign"

    async def one_cycle(seconds, stop_event):
        return True

    async def nothing(*a, **k):
        return {}

    monkeypatch.setattr(main_mod, "decide_next_action", write)
    monkeypatch.setattr(main_mod, "_interruptible_sleep", one_cycle)
    monkeypatch.setattr(main_mod, "run_tick_steps", nothing)
    import sellbird.events
    import sellbird.mentions
    import sellbird.texting
    monkeypatch.setattr(sellbird.texting, "tick", nothing)
    monkeypatch.setattr(sellbird.mentions, "tick", nothing)
    monkeypatch.setattr(sellbird.events, "deliver_pending", nothing)
    _fake_agents(monkeypatch, record, both_started)
    return record


@pytest.mark.asyncio
async def test_two_projects_run_side_by_side_without_leaking(tmp_path, quiet_loop):
    a = make_project(tmp_path, "product-a", "Example Roofing")
    b = make_project(tmp_path, "segment-b", "Example Dental")
    await asyncio.wait_for(main_mod.run_projects([a, b], asyncio.Event()), timeout=30)

    assert quiet_loop["product-a"] == {
        "config": "Example Roofing", "loaded": "Example Roofing",
        "thread": ("product-a", "Example Roofing"), "db": a.db_path}
    assert quiet_loop["segment-b"] == {
        "config": "Example Dental", "loaded": "Example Dental",
        "thread": ("segment-b", "Example Dental"), "db": b.db_path}
    for home, company in ((a, "Example Roofing"), (b, "Example Dental")):
        drafts = await StateManager(str(home.db_path)).get_campaigns_by_status("draft")
        assert [d.name for d in drafts] == [f"draft for {company}"]
    assert (tmp_path / "data" / "shared.db").exists()


@pytest.mark.asyncio
async def test_a_project_whose_config_fails_doesnt_stop_the_others(tmp_path, quiet_loop):
    a = make_project(tmp_path, "product-a", "Example Roofing")
    b = make_project(tmp_path, "segment-b", "Example Dental")
    b.config_file.write_text("persona: [not, a, mapping\n", encoding="utf-8")
    quiet_loop_started = asyncio.Event()

    # A's Writer waits for a second project to start; only A runs here.
    import sellbird.agents.writer as writer_mod
    real = writer_mod.Writer.run

    async def alone(self):
        quiet_loop_started.set()
        await self.state.add_campaign(Campaign(
            id="", name="draft", channel="email", sequence=[EmailStep(step=1, subject="s", body="b")],
            prospect_ids=[], status="draft"))
    writer_mod.Writer.run = alone
    try:
        await asyncio.wait_for(main_mod.run_projects([a, b], asyncio.Event()), timeout=30)
    finally:
        writer_mod.Writer.run = real
    assert quiet_loop_started.is_set()
    assert len(await StateManager(str(a.db_path)).get_campaigns_by_status("draft")) == 1
    assert not b.db_path.exists()


# ── sellbird run: locks, and no projects/ folder ──

@pytest.fixture
def install(tmp_path, monkeypatch):
    from sellbird import paths
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return tmp_path


def test_run_in_projects_mode_holds_the_roots_lock_and_skips_what_cant_run(install, monkeypatch, capsys):
    make_project(install, "product-a", "Example Roofing")
    make_project(install, "segment-b", "Example Dental")
    make_project(install, "not-trained", "Your Company")
    legacy = make_project(install, "legacy-c", "Example Legacy")
    legacy.config_file.write_text(trained("Example Legacy").replace(
        'provider: "gmail"', 'provider: "instantly"', 1), encoding="utf-8")
    monkeypatch.setattr(main_mod, "LOCK_FILE", install / "data" / "sellbird.lock")
    monkeypatch.setattr(pidfile, "PID_FILE", install / "data" / "sellbird.pid")
    seen = {}

    async def run(homes=None):
        seen["homes"] = [h.project for h in homes]
        seen["root"] = pidfile.loop_lock_held(install / "data" / "sellbird.lock")
        seen["pid"] = pidfile.read_pid()

    monkeypatch.setattr(main_mod, "_run_with_signals", run)
    main_mod.main()
    assert seen["homes"] == ["product-a", "segment-b"]     # untrained and Instantly skipped
    assert seen["root"] is True and seen["pid"]
    out = capsys.readouterr().out
    assert "legacy-c sends through Instantly" in out and "not-trained isn't set up" in out
    # Released and removed afterwards; no project gets a lock of its own.
    assert not pidfile.loop_lock_held(install / "data" / "sellbird.lock")
    assert not (install / "data" / "sellbird.pid").exists()
    assert not (install / "projects" / "product-a" / "data" / "sellbird.lock").exists()


def test_a_second_run_refuses_while_the_first_runs(install, monkeypatch, capsys):
    make_project(install, "product-a", "Example Roofing")
    lock = install / "data" / "sellbird.lock"
    monkeypatch.setattr(main_mod, "LOCK_FILE", lock)
    ran = []

    async def run(homes=None):
        ran.append(homes)

    monkeypatch.setattr(main_mod, "_run_with_signals", run)
    held = pidfile.hold_loop_lock(lock)
    try:
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
    finally:
        pidfile.release_loop_lock(held)
    assert exc.value.code == 1 and ran == []
    assert "already running" in capsys.readouterr().out


def test_no_projects_folder_runs_one_heartbeat_as_before(install, monkeypatch):
    (install / "sellbird.yaml").write_text(trained("Example Roofing"), encoding="utf-8")
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)   # as tests/test_pidfile.py does
    monkeypatch.setattr(main_mod, "LOCK_FILE", install / "data" / "sellbird.lock")
    monkeypatch.setattr(main_mod, "write_pid_file", lambda *a: None)
    monkeypatch.setattr(main_mod, "remove_pid_file", lambda *a: None)
    seen = []

    async def run(homes=None):
        seen.append(homes)
        state = StateManager(str(install / "data" / "sellbird.db"))
        await state.init_db()
        await state.add_suppression("address", "pat@acme.example", "opted_out")
        seen.append(state.shared)

    monkeypatch.setattr(main_mod, "_run_with_signals", run)
    main_mod.main()
    assert seen == [None, None]
    assert not (install / "data" / "shared.db").exists()
    assert not (install / "projects").exists()
