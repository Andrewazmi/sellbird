"""Each inbox signs as its own persona, per project (docs/projects.md).

Inboxes are a project's own rows, and the main mailbox's names are its
project's persona, so one address can sign as a different name in each
project. Two projects running in one process never sign as each other.
Every name and address here is a placeholder.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sellbird import extensions, mailboxes
from sellbird.mailboxes import MailboxStore
from sellbird.state import StateManager
from sellbird.workspace_home import Home, current_home, use_home

from tests.test_inbox_persona import signed_campaign
from tests.test_mailboxes import Box
from tests.test_outbox_native import Cfg, Env, seed_prospect
from tests.test_projects_run import TEMPLATE

SIDE = "two@side.example"
NAMES = {   # project: (main mailbox's sign-off name, the side inbox's sign-off name)
    "alpha": ("Alex Alpha", "Sam Alpha"),
    "beta": ("أليكس بيتا", "سام بيتا"),
}


class Recording(Box):
    """A fake inbox that keeps who each email went to and what it said."""

    def __init__(self, address, **kw):
        super().__init__(address, **kw)
        self.mail = []

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **_):
        self.mail.append((to_email, body))
        await asyncio.sleep(0)            # let the other project's Sender run in between
        return await super().send_email(to_email, subject, body, thread_ref, in_reply_to)


@pytest.fixture
def side_box(monkeypatch):
    """One fake provider per address, shared by every project, as a real
    mailbox is."""
    made: dict[str, Recording] = {}
    monkeypatch.setattr(extensions, "_REGISTRY", extensions.Registry())
    monkeypatch.setattr(extensions, "_LOADED", True)

    def build(row, *, config, env, secrets):
        return made.setdefault(row["address"], Recording(row["address"]))

    extensions._REGISTRY.add_mailbox_connection("fake", build)
    return made


def project_cfg(project: str):
    """A Cfg whose main mailbox signs as this project's persona."""
    main_name = NAMES[project][0]

    class ProjectCfg(Cfg):
        class persona(Cfg.persona):
            sign_off_name = main_name

        class channels(Cfg.channels):
            class email(Cfg.channels.email):
                require_approval = False

    return ProjectCfg()


async def _add_side_inbox(store: MailboxStore, sign_off: str) -> str:
    mid = mailboxes._new_id()
    await store.add_mailbox(mailbox_id=mid, address=SIDE, provider="fake", connection="fake")
    await store.set_sign_off_name(mid, sign_off)
    return mid


# ── sending: two projects in one process ──

@pytest.mark.asyncio
async def test_one_address_signs_as_each_projects_own_name_with_no_crossing(tmp_path, side_box):
    from sellbird.agents.sender import Sender
    main = Recording("sellbird@x.co")          # the main mailbox is shared too
    senders = {}
    for project, (_, side_name) in NAMES.items():
        with use_home(Home.for_project(tmp_path, project)):
            Home.for_project(tmp_path, project).root.mkdir(parents=True, exist_ok=True)
            state = StateManager()
            await state.init_db()
            await _add_side_inbox(MailboxStore(db_path=state.db_path), side_name)
            pids = [await seed_prospect(state, email=f"p{i}@{project}{i}.example") for i in range(4)]
            await signed_campaign(state, pids)
            sender = Sender(brain=None, state=state, config=project_cfg(project), env=Env())
            sender.provider = main
            sender.send_pacing = False
            senders[project] = sender

    async def run(project):
        assert current_home().project == project
        for _ in range(2):                       # two drains each, interleaved
            await senders[project]._run_native()
            await asyncio.sleep(0)
        assert current_home().project == project

    tasks = []
    for project in NAMES:
        with use_home(Home.for_project(tmp_path, project)):
            tasks.append(asyncio.create_task(run(project)))
    await asyncio.gather(*tasks)

    side = side_box[SIDE]
    for box, which in ((main, 0), (side, 1)):
        assert {to.split("@")[1][:-len("0.example")] for to, _ in box.mail} == set(NAMES), box.address
        for to, body in box.mail:
            project = to.split("@")[1][:-len("0.example")]
            name = NAMES[project][which]
            other = NAMES["beta" if project == "alpha" else "alpha"][which]
            assert f"I'm {name}." in body and body.split("\n\n")[1] == name.split(" ")[0], (to, body)
            assert other not in body and other.split(" ")[0] not in body
    assert len(main.mail) + len(side.mail) == 8

    # What each project stored is what it sent: its own names, no tag left.
    for project in NAMES:
        db = tmp_path / "projects" / project / "data" / "sellbird.db"
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT to_email, body FROM outbox WHERE status = 'sent'").fetchall()
        conn.close()
        assert len(rows) == 4
        assert all(to.endswith(".example") and project in to for to, _ in rows)
        assert not any("{{" in body for _, body in rows)


# ── `sellbird inboxes name` takes the project like other data commands ──

@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    from sellbird import home, paths
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(home, "seed_home", lambda *a, **k: None)
    monkeypatch.delenv("SELLBIRD_PROJECT", raising=False)
    ids = {}
    for project in NAMES:
        h = Home.for_project(tmp_path, project)
        h.root.mkdir(parents=True, exist_ok=True)
        h.config_file.write_text(TEMPLATE, encoding="utf-8")
        store = MailboxStore(db_path=str(h.db_path))
        asyncio.run(store.init_db())
        ids[project] = asyncio.run(_add_side_inbox(store, ""))
    return tmp_path, ids


def _sellbird(monkeypatch, *argv) -> int:
    from sellbird import cli
    monkeypatch.setattr(sys, "argv", ["sellbird", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


def _row(root: Path, project: str, mid: str) -> dict:
    store = MailboxStore(db_path=str(root / "projects" / project / "data" / "sellbird.db"))
    return asyncio.run(store.get_mailbox(mid))


def _persona(root: Path, project: str) -> dict:
    path = root / "projects" / project / "sellbird.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["persona"]


def test_inboxes_name_works_on_the_project_asked_for(two_projects, monkeypatch, capsys):
    root, ids = two_projects
    alpha_side, beta_side = NAMES["alpha"][1], NAMES["beta"][1]
    assert _sellbird(monkeypatch, "--project", "alpha", "inboxes", "name", SIDE,
                     "--sign-off", alpha_side) == 0
    monkeypatch.setenv("SELLBIRD_PROJECT", "beta")
    assert _sellbird(monkeypatch, "inboxes", "name", SIDE, "--sign-off", beta_side) == 0
    assert _row(root, "alpha", ids["alpha"])["sign_off_name"] == alpha_side
    assert _row(root, "beta", ids["beta"])["sign_off_name"] == beta_side

    # The main mailbox's sign-off goes in that project's own sellbird.yaml,
    # never a sellbird.local.yaml beside it or the root's.
    assert _sellbird(monkeypatch, "inboxes", "name", "main", "--sign-off", NAMES["beta"][0]) == 0
    assert _persona(root, "beta")["sign_off_name"] == NAMES["beta"][0]
    assert "sign_off_name" not in _persona(root, "alpha")
    assert not list(root.rglob("sellbird.local.yaml")) and not (root / "sellbird.yaml").exists()
    capsys.readouterr()

    # Without a project it's refused, and nothing changes.
    monkeypatch.delenv("SELLBIRD_PROJECT")
    assert _sellbird(monkeypatch, "inboxes", "name", SIDE, "--sign-off", "Someone Else") == 2
    assert "Say which one" in capsys.readouterr().out
    assert _row(root, "alpha", ids["alpha"])["sign_off_name"] == alpha_side
    assert _row(root, "beta", ids["beta"])["sign_off_name"] == beta_side


# ── the dashboard: the request's project ──

HEADER = "X-Sellbird-Project"


@pytest.fixture
def client(two_projects, monkeypatch):
    import sellbird.dashboard as dash
    root, ids = two_projects
    (root / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(dash, "PROJECT_ROOT", root)
    monkeypatch.setattr(dash, "DB_PATH", root / "data" / "sellbird.db")
    monkeypatch.setattr(dash, "PID_FILE", root / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOCK_FILE", root / "data" / "sellbird.lock")
    monkeypatch.setattr(dash, "LOG_FILE", root / "data" / "sellbird.log")
    monkeypatch.setattr(dash, "ENV_FILE", root / ".env")
    monkeypatch.setattr(dash, "_discovery_runs", {})
    dash.token_store.configure(root / "data" / "dashboard-token")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        yield c, root, ids


def test_the_dashboard_signs_and_names_inboxes_in_the_requests_project(client):
    c, root, ids = client
    for project in NAMES:
        main_name, side_name = NAMES[project]
        h = {HEADER: project}
        assert c.post(f"/api/inboxes/{ids[project]}/name", headers=h,
                      json={"sign_off_name": side_name}).json()["success"]
        assert c.post("/api/inboxes/main/name", headers=h,
                      json={"sign_off_name": main_name}).json()["success"]
        store = MailboxStore(db_path=str(root / "projects" / project / "data" / "sellbird.db"))
        for mid in (ids[project], ""):
            asyncio.run(store.add_outbox_item(
                prospect_id="p1", to_email=f"jane@{project}{mid[:4]}.example", subject="plan",
                mailbox_id=mid, body="Want a plan?\n\n{{sender_name}}", send_at="2000-01-01"))

    # Each project's own sellbird.yaml, no local copy.
    assert not list(root.rglob("sellbird.local.yaml"))
    for project in NAMES:
        assert _persona(root, project)["sign_off_name"] == NAMES[project][0]

    # Interleaved requests, each signed as its own project's inbox.
    for project in ("alpha", "beta", "alpha", "beta"):
        main_name, side_name = NAMES[project]
        pending = c.get("/api/outbox", headers={HEADER: project}).json()["pending"]
        signed = {d["from_address"]: d["body"] for d in pending}
        assert signed == {SIDE: f"Want a plan?\n\n{side_name}",
                          "alex@yourcompany.com": f"Want a plan?\n\n{main_name}"}, project
        inboxes = c.get("/api/inboxes", headers={HEADER: project}).json()["inboxes"]
        assert {i["address"]: i["signs_as"] for i in inboxes} == {
            SIDE: side_name, "alex@yourcompany.com": main_name}
