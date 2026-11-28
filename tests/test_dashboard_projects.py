"""The dashboard in projects mode (docs/projects.md, "The dashboard"): one
process, one login, and a project picked per request by the
X-Sellbird-Project header, else the sellbird_project cookie, else the default.
Every name here is a placeholder."""

import asyncio
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird import pidfile, projects
from sellbird.state import StateManager

HEADER = "X-Sellbird-Project"
YAML = "persona:\n  name: Sam Example\n  company: Example Co\n"


def _seed(db: Path, tag: str, n: int = 2) -> None:
    """n pending drafts, each addressed to <tag>-<i>@example.com."""
    asyncio.run(StateManager(str(db)).init_db())
    with sqlite3.connect(db) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO outbox (id, kind, to_email, subject, body, status) "
                "VALUES (?, 'sequence', ?, ?, ?, 'pending_review')",
                (f"{tag}-{i}", f"{tag}-{i}@example.com", f"Hello from {tag}", f"Body for {tag}"))
    conn.close()


def _rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT id, status, subject FROM outbox ORDER BY id").fetchall()
    conn.close()
    return rows


def _tree(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A root holding two projects, alpha and beta, each with its own database
    and drafts; the dashboard is pointed at it."""
    root = tmp_path / "install"
    for name in ("alpha", "beta"):
        folder = root / "projects" / name
        (folder / "data").mkdir(parents=True)
        (folder / "sellbird.yaml").write_text(YAML, encoding="utf-8")
        _seed(folder / "data" / "sellbird.db", name)
    (root / "data").mkdir()
    monkeypatch.setattr(dash, "PROJECT_ROOT", root)
    monkeypatch.setattr(dash, "DB_PATH", root / "data" / "sellbird.db")
    monkeypatch.setattr(dash, "PID_FILE", root / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOCK_FILE", root / "data" / "sellbird.lock")
    monkeypatch.setattr(dash, "ENV_FILE", root / ".env")
    monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "sellbird" / "web")
    monkeypatch.setattr(dash, "_discovery_runs", {})
    dash.token_store.configure(root / "data" / "dashboard-token")
    return root


def _db(root: Path, name: str) -> Path:
    return root / "projects" / name / "data" / "sellbird.db"


@pytest.fixture
def client(install):
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def _subjects(client, project=None) -> set[str]:
    headers = {HEADER: project} if project else {}
    data = client.get("/api/outbox", headers=headers).json()
    return {d["to_email"] for d in data["pending"]}


# ── Which project a request is for ──


def test_the_header_picks_the_project(client):
    assert _subjects(client, "alpha") == {"alpha-0@example.com", "alpha-1@example.com"}
    assert _subjects(client, "beta") == {"beta-0@example.com", "beta-1@example.com"}


def test_a_request_with_no_name_uses_the_default_project(client, install):
    assert _subjects(client) == {"alpha-0@example.com", "alpha-1@example.com"}   # first by name
    (install / "sellbird.projects.yaml").write_text("default: beta\n", encoding="utf-8")
    assert _subjects(client) == {"beta-0@example.com", "beta-1@example.com"}
    assert client.get("/api/projects").json()["default"] == "beta"


def test_the_cookie_is_used_when_there_is_no_header(client):
    r = client.post("/api/projects/select", json={"project": "beta"})
    assert r.status_code == 200 and r.json() == {"success": True, "project": "beta"}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert _subjects(client) == {"beta-0@example.com", "beta-1@example.com"}
    # An explicit header still wins over the cookie.
    assert _subjects(client, "alpha") == {"alpha-0@example.com", "alpha-1@example.com"}


def test_selecting_a_project_that_does_not_exist_sets_no_cookie(client):
    r = client.post("/api/projects/select", json={"project": "nope"})
    assert r.status_code == 404 and "set-cookie" not in r.headers
    assert client.post("/api/projects/select", json={}).status_code == 404
    assert client.post("/api/projects/select", json={"project": "../etc"}).status_code == 404


def test_projects_lists_names_default_and_current(client):
    data = client.get("/api/projects", headers={HEADER: "beta"}).json()
    assert data["projects"] == ["alpha", "beta"]
    assert data["default"] == "alpha" and data["current"] == "beta"
    assert client.get("/api/projects").json()["current"] == "alpha"


# ── An unknown name is a 404, never another project ──


def test_an_unknown_project_is_a_404_and_writes_nothing(client, install):
    before = _tree(install)
    for method, path, body in (
        ("GET", "/api/outbox", None),
        ("GET", "/api/stats", None),
        ("POST", "/api/outbox/alpha-0/approve", {}),
        ("POST", "/api/outbox/approve-all", {}),
        ("POST", "/api/outbox/alpha-0/edit", {"subject": "x", "body": "y"}),
        ("POST", "/api/feedback", {"entity_type": "t", "entity_id": "1", "comment": "c"}),
    ):
        r = client.request(method, path, json=body, headers={HEADER: "gamma"})
        assert r.status_code == 404, (path, r.status_code)
        assert r.json()["error"] == "unknown_project"
    for bad in ("../alpha", "ALPHA", "alpha/", "a" * 41, "alpha beta"):
        assert client.get("/api/outbox", headers={HEADER: bad}).status_code == 404
    assert _tree(install) == before
    assert {s for _, s, _ in _rows(_db(install, "alpha"))} == {"pending_review"}
    assert {s for _, s, _ in _rows(_db(install, "beta"))} == {"pending_review"}


def test_an_unknown_name_never_creates_a_database_or_folder(client, install):
    client.get("/api/outbox", headers={HEADER: "gamma"})
    assert not (install / "projects" / "gamma").exists()
    assert not (install / "data" / "sellbird.db").exists()


def test_the_page_and_project_list_still_load_with_a_stale_cookie(client):
    """A cookie for a project that was removed must not lock the page out; the
    page asks which projects exist and picks one."""
    client.cookies.set("sellbird_project", "gone")
    assert client.get("/").status_code == 200
    data = client.get("/api/projects").json()
    assert data["projects"] == ["alpha", "beta"] and data["current"] == "alpha"
    assert client.get("/api/outbox").status_code == 404               # data is never guessed
    assert client.get("/api/outbox", headers={HEADER: "beta"}).status_code == 200


def test_only_a_signed_in_caller_learns_whether_a_project_exists(install):
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as anon:
        for name in ("alpha", "gamma"):
            r = anon.get("/api/outbox", headers={HEADER: name})
            assert r.status_code == 401, (name, r.status_code)


# ── A change under one project never touches another ──


def test_approving_under_one_project_leaves_the_other_alone(client, install):
    r = client.post("/api/outbox/alpha-0/approve", headers={HEADER: "alpha"})
    assert r.json() == {"success": True}
    assert dict((i, s) for i, s, _ in _rows(_db(install, "alpha")))["alpha-0"] == "approved"
    assert {s for _, s, _ in _rows(_db(install, "beta"))} == {"pending_review"}
    # The same id under the wrong project is not found, and changes nothing.
    r = client.post("/api/outbox/alpha-1/approve", headers={HEADER: "beta"})
    assert r.json() == {"success": False}
    assert dict((i, s) for i, s, _ in _rows(_db(install, "alpha")))["alpha-1"] == "pending_review"


def test_approve_all_and_edit_stay_in_their_project(client, install):
    r = client.post("/api/outbox/beta-0/edit", headers={HEADER: "beta"},
                    json={"subject": "A new subject", "body": "A new body, short and plain."})
    assert r.status_code == 200, r.text
    assert any(s == "A new subject" for _, _, s in _rows(_db(install, "beta")))
    assert all(s == "Hello from alpha" for _, _, s in _rows(_db(install, "alpha")))
    client.post("/api/outbox/approve-all", headers={HEADER: "alpha"})
    assert {s for _, s, _ in _rows(_db(install, "alpha"))} == {"approved"}
    assert {s for _, s, _ in _rows(_db(install, "beta"))} == {"pending_review"}


def test_feedback_is_written_to_the_requests_project(client, install):
    client.post("/api/feedback", headers={HEADER: "beta"},
                json={"entity_type": "company", "entity_id": "c1", "comment": "only in beta"})
    assert client.get("/api/feedback/company/c1", headers={HEADER: "beta"}).json()[0]["comment"] == "only in beta"
    assert client.get("/api/feedback/company/c1", headers={HEADER: "alpha"}).json() == []


def test_two_interleaved_streams_do_not_leak(install):
    """Requests for two projects in flight at once, in one process."""
    token = dash.token_store.get()

    async def go():
        transport = httpx.ASGITransport(app=dash.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5555",
                                     headers={"Authorization": f"Bearer {token}"}) as c:
            async def ask(project: str):
                r = await c.get("/api/outbox", headers={HEADER: project})
                return project, {d["to_email"].split("-")[0] for d in r.json()["pending"]}

            results = await asyncio.gather(*[ask("alpha" if i % 2 == 0 else "beta") for i in range(40)])
            return results

    results = asyncio.run(go())
    assert len(results) == 40
    for project, tags in results:
        assert tags == {project}, (project, tags)


# ── Outside projects mode nothing changes ──


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """An ordinary install: no projects/ folder."""
    root = tmp_path / "plain"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(dash, "PROJECT_ROOT", root)
    monkeypatch.setattr(dash, "DB_PATH", root / "data" / "sellbird.db")
    monkeypatch.setattr(dash, "PID_FILE", root / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOCK_FILE", root / "data" / "sellbird.lock")
    monkeypatch.setattr(dash, "_discovery_runs", {})
    dash.token_store.configure(root / "data" / "dashboard-token")
    _seed(root / "data" / "sellbird.db", "solo")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.root = root
        yield c


def test_outside_projects_mode_the_list_is_empty(plain):
    assert plain.get("/api/projects").json() == {
        "projects": [], "default": None, "current": None}


def test_outside_projects_mode_routes_work_as_before(plain):
    assert {d["to_email"] for d in plain.get("/api/outbox").json()["pending"]} == {
        "solo-0@example.com", "solo-1@example.com"}
    # A header is ignored: there is nothing to pick, and no 404.
    r = plain.get("/api/outbox", headers={HEADER: "whatever"})
    assert r.status_code == 200 and len(r.json()["pending"]) == 2
    assert plain.post("/api/outbox/solo-0/approve").json() == {"success": True}
    assert plain.get("/api/sellbird/status").json().keys() == {
        "running", "pid", "started_at", "sending_paused"}


def test_a_projects_folder_with_no_project_in_it_is_not_projects_mode(plain):
    (plain.root / "projects" / "empty").mkdir(parents=True)
    assert plain.get("/api/projects").json()["projects"] == []
    assert plain.get("/api/outbox", headers={HEADER: "x"}).status_code == 200


# ── The loop: one process for every project ──


def _hold(lock: Path, pid_file: Path):
    handle = pidfile.hold_loop_lock(lock)
    assert handle is not None
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    return handle


def test_the_one_loop_reads_as_running_in_every_project(client, install):
    assert client.get("/api/sellbird/status", headers={HEADER: "alpha"}).json()["running"] is False
    handle = _hold(install / "data" / "sellbird.lock", install / "data" / "sellbird.pid")
    try:
        for name in ("alpha", "beta"):
            status = client.get("/api/sellbird/status", headers={HEADER: name}).json()
            assert status["running"] is True and status["pid"] == os.getpid()
            assert status["project"] == name
        # Start refuses while it runs (nothing is launched).
        r = client.post("/api/sellbird/start", headers={HEADER: "beta"}).json()
        assert r == {"success": False, "message": "Sellbird is already running."}
    finally:
        pidfile.release_loop_lock(handle)


def test_a_stale_pid_file_is_removed_and_never_signalled(client, install):
    pid_file = install / "data" / "sellbird.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")        # alive, but no lock held
    assert client.get("/api/sellbird/status", headers={HEADER: "alpha"}).json()["running"] is False
    assert not pid_file.exists()


def test_start_in_projects_mode_does_not_need_this_tabs_project_trained(client, install, monkeypatch):
    """The loop skips a project that won't load, so Start asks only for the
    shared .env."""
    launched = []
    monkeypatch.setattr(dash.subprocess, "Popen", lambda *a, **k: launched.append((a, k)) or
                        type("P", (), {"pid": 1, "poll": lambda self: None})())
    r = client.post("/api/sellbird/start", headers={HEADER: "alpha"}).json()
    assert r["success"] is False and "isn't set up" in r["message"] and not launched   # no .env yet
    (install / ".env").write_text("X=1\n", encoding="utf-8")
    r = client.post("/api/sellbird/start", headers={HEADER: "alpha"}).json()
    assert r["success"] is True and len(launched) == 1, r
    args, kwargs = launched[0]
    assert args[0][-2:] == ["sellbird", "run"] and kwargs["cwd"] == str(install)
    dash._sellbird_process = None


# ── What is per project and what is shared ──


def test_suppression_counts_include_the_shared_list(client, install):
    from sellbird.shared import SharedState
    shared = SharedState(install / "data")
    asyncio.run(shared.add_suppression("address", "out@example.com", "opted_out", "test"))
    asyncio.run(shared.add_suppression("address", "bounce@example.com", "bounced", "test"))
    asyncio.run(StateManager(str(_db(install, "alpha"))).init_db())
    with sqlite3.connect(_db(install, "alpha")) as conn:
        conn.execute("INSERT INTO suppressions (id, kind, value, reason) VALUES "
                     "('s1', 'address', 'mine@example.com', 'manual')")
        # The same person on both lists counts once.
        conn.execute("INSERT INTO suppressions (id, kind, value, reason) VALUES "
                     "('s2', 'address', 'out@example.com', 'opted_out')")
    conn.close()
    data = client.get("/api/suppressions", headers={HEADER: "alpha"}).json()
    assert data["counts"] == {"opted_out": 1, "bounced": 1, "manual": 1}
    assert data["total"] == 3
    # Another project sees the shared two, not alpha's own.
    other = client.get("/api/suppressions", headers={HEADER: "beta"}).json()
    assert other["counts"] == {"opted_out": 1, "bounced": 1} and other["total"] == 2


def test_signals_to_confirm_are_each_projects_own(client, install):
    """A new project's own database seeds the catalog as proposed; what
    another project confirmed never shows there (seen live: 29 to confirm in
    a new project beside 26 confirmed in the first)."""
    for name in ("alpha", "beta"):         # the Signals screen seeds its project's catalog
        assert client.get("/api/signals", headers={HEADER: name}).status_code == 200
    with sqlite3.connect(_db(install, "alpha")) as conn:
        conn.execute("UPDATE signal_codes SET status = 'confirmed'")
    conn.close()

    def signals(project):
        checks = client.get("/api/setup-status", headers={HEADER: project}).json()["checks"]
        return next(c for c in checks if c["id"] == "signals")
    a, b = signals("alpha"), signals("beta")
    assert a["confirmed"] > 0 and a["proposed"] == 0
    assert b["confirmed"] == 0 and b["proposed"] == a["confirmed"]
    assert not (install / "data" / "sellbird.db").exists()      # never the root's


def test_a_discovery_run_in_one_project_is_not_shown_in_another(client):
    dash._discovery_runs["alpha"] = {"task": None, "report": {"profiled_companies": 7}, "kind": "profile"}
    a = client.get("/api/discover/providers", headers={HEADER: "alpha"}).json()
    b = client.get("/api/discover/providers", headers={HEADER: "beta"}).json()
    assert a["last_report"] == {"profiled_companies": 7} and a["running_kind"] == "profile"
    assert b["last_report"] is None and b["running_kind"] == "discover"


def test_the_offer_is_saved_in_the_projects_own_file(client, install):
    """A project's sellbird.yaml is the only config it reads, so the offer must
    go there and not into a sellbird.local.yaml it would never load."""
    from sellbird.workspace_home import use_home
    from sellbird.projects import project_home
    with use_home(project_home(install, "beta")):
        assert dash._offer_config_file() == install / "projects" / "beta" / "sellbird.yaml"
    assert not (install / "projects" / "beta" / "sellbird.local.yaml").exists()


def test_setup_status_looks_for_the_projects_own_training(client, install):
    def trained(project):
        checks = client.get("/api/setup-status", headers={HEADER: project}).json()["checks"]
        return next(c["done"] for c in checks if c["id"] == "product_trained")
    assert trained("alpha") is False
    skills = install / "projects" / "alpha" / "skills"
    skills.mkdir()
    (skills / "product_knowledge.md").write_text("# Example product\n", encoding="utf-8")
    assert trained("alpha") is True and trained("beta") is False
    root_skills = install / "skills"
    root_skills.mkdir()
    (root_skills / "product_knowledge.md").write_text("# Product A\n", encoding="utf-8")
    assert trained("beta") is False       # training is a project's own, never the root's

    # Your company shows only the project's own training.
    from tests.test_projects_run import trained as trained_config
    for name in ("alpha", "beta"):
        (install / "projects" / name / "sellbird.yaml").write_text(
            trained_config("Example Co"), encoding="utf-8")
    assert client.get("/api/company", headers={HEADER: "beta"}).json()["knowledge"] == ""
    assert "Example product" in client.get("/api/company", headers={HEADER: "alpha"}).json()["knowledge"]


# ── The MCP server pinned to a project ──


class _Recorder(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.ASGITransport(app=dash.app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


@pytest.fixture
def agent(install, monkeypatch):
    from sellbird import dashboard_instance
    from sellbird.dashboard_auth import load_or_create_token
    data = install / "data"
    load_or_create_token(data / "agent-token")
    dash.agent_token_store.configure(data / "agent-token")
    dashboard_instance.write(data, host="127.0.0.1", port=5555, instance=dash.INSTANCE_ID)
    yield data
    dash.agent_token_store.configure(dash.AGENT_TOKEN_FILE)


def _agent_call(agent, project, tool="outbox_list"):
    import json
    from sellbird import mcp_server
    transport = _Recorder()
    client = mcp_server.CoreClient(agent, transport=transport, project=project)
    server = mcp_server.build_server(lambda: client)

    async def go():
        blocks = await server.call_tool(tool, {})
        return json.loads(blocks.content[0].text)

    return asyncio.run(go()), transport


def test_a_pinned_mcp_server_sends_the_header_and_sees_only_its_project(agent):
    out, transport = _agent_call(agent, "beta")
    assert {d["to_email"] for d in out["pending"]} == {"beta-0@example.com", "beta-1@example.com"}
    sent = [r for r in transport.requests if r.url.path == "/api/outbox"]
    assert sent and all(r.headers[HEADER] == "beta" for r in sent)
    out, _ = _agent_call(agent, "alpha")
    assert {d["to_email"].split("-")[0] for d in out["pending"]} == {"alpha"}


def test_a_pinned_mcp_server_never_falls_back_to_another_project(agent, install):
    out, _ = _agent_call(agent, "gamma")
    assert out["ok"] is False and out["status"] == 404 and out["error"] == "unknown_project"


def test_an_unpinned_mcp_server_sends_no_header(agent):
    out, transport = _agent_call(agent, None)
    assert all(HEADER not in r.headers for r in transport.requests)
    assert {d["to_email"].split("-")[0] for d in out["pending"]} == {"alpha"}     # the default


def test_the_project_comes_from_the_environment(monkeypatch):
    from sellbird import mcp_server
    assert mcp_server.project_from_env({}) is None
    assert mcp_server.project_from_env({"SELLBIRD_PROJECT": " beta "}) == "beta"
    with pytest.raises(Exception):
        mcp_server.project_from_env({"SELLBIRD_PROJECT": "Bad Name\r\nX: y"})
    monkeypatch.setenv("SELLBIRD_PROJECT", "beta")
    assert mcp_server.default_client().project == "beta"


def test_turning_the_agent_off_in_one_project_cuts_it_off_in_every_project(agent, client):
    # One agent token serves the whole install, so its switch is the install's.
    assert client.post("/api/agent/access", json={"access": "off"},
                       headers={HEADER: "alpha"}).json()["access"] == "off"
    for name in ("alpha", "beta"):
        out, _ = _agent_call(agent, name)
        assert out["ok"] is False and out["status"] == 403, name
    assert client.get("/api/agent", headers={HEADER: "beta"}).json()["access"] == "off"
    # Letting it back in from either project lets it back in everywhere.
    assert client.post("/api/agent/access", json={"access": "on"},
                       headers={HEADER: "beta"}).json()["access"] == "on"
    out, _ = _agent_call(agent, "alpha")
    assert {d["to_email"].split("-")[0] for d in out["pending"]} == {"alpha"}


def test_in_projects_mode_only_the_installs_switch_counts(agent, client, install):
    """A project's own leftover switch is ignored: `projects init` carries an
    off switch into shared.db, and from then on there is one switch."""
    asyncio.run(StateManager(str(_db(install, "beta"))).set_setting(dash.AGENT_ACCESS_KEY, "off"))
    out, _ = _agent_call(agent, "beta")
    assert {d["to_email"].split("-")[0] for d in out["pending"]} == {"beta"}
    client.post("/api/agent/access", json={"access": "off"}, headers={HEADER: "alpha"})
    for project in ("alpha", "beta"):
        out, _ = _agent_call(agent, project)
        assert out["status"] == 403
    client.post("/api/agent/access", json={"access": "on"}, headers={HEADER: "beta"})
    out, _ = _agent_call(agent, "alpha")
    assert {d["to_email"].split("-")[0] for d in out["pending"]} == {"alpha"}


def test_sellbird_mcp_project_pins_the_server(monkeypatch):
    import sys
    from sellbird import cli, mcp_server
    seen = []
    monkeypatch.setattr(mcp_server, "run_stdio", lambda project=None: seen.append(project))
    monkeypatch.setattr(sys, "argv", ["sellbird", "mcp", "--project", "beta"])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["sellbird", "mcp"])
    cli.main()
    assert seen == ["beta", None]


# ── Inboxes: each project's own database (review B3) ──


def test_the_inboxes_page_reads_the_projects_database(client, install):
    import shutil
    template = Path(__file__).resolve().parent.parent / "sellbird.yaml"
    shutil.copy(template, install / "projects" / "alpha" / "sellbird.yaml")
    asyncio.run(StateManager(str(_db(install, "alpha"))).set_setting("sending_paused", "alpha-paused"))
    r = client.get("/api/inboxes", headers={HEADER: "alpha"})
    assert r.status_code == 200, r.text
    assert r.json()["sending_paused"] == "alpha-paused"
    assert not (install / "data" / "sellbird.db").exists()


def test_a_shared_inbox_pause_shows_in_every_projects_view(client, install):
    """Review I-b: an inbox one project's bounce rate paused is marked on
    every project's Inboxes page, sending status and limits."""
    import shutil
    from sellbird.shared import SharedState
    template = Path(__file__).resolve().parent.parent / "sellbird.yaml"
    for name in ("alpha", "beta"):
        shutil.copy(template, install / "projects" / name / "sellbird.yaml")
    asyncio.run(SharedState(install / "data").pause_inbox(
        "alex@yourcompany.com", "alpha", "bounce rate 3/20"))
    for name in ("alpha", "beta"):
        inboxes = client.get("/api/inboxes", headers={HEADER: name}).json()
        [main] = [i for i in inboxes["inboxes"] if i["main"]]
        assert main["paused_reason"] == "paused for every project by alpha: bounce rate 3/20"
        assert inboxes["project"] == name
        status = client.get("/api/sellbird/status", headers={HEADER: name}).json()
        assert status["inbox_pauses"] == [{"inbox": "alex@yourcompany.com", "project": "alpha",
                                           "reason": "bounce rate 3/20"}]
        limits = client.get("/api/limits", headers={HEADER: name}).json()
        assert [p["project"] for p in limits["inbox_pauses"]] == ["alpha"]


def test_the_pause_banners_say_whose_pause_it_is():
    """In projects mode a project's own pause isn't "All sending is paused"."""
    js = (Path(dash.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    assert "data.project ? 'Sending is paused for this project'" in js
    assert "currentProject() ? 'Sending is paused for this project'" in js
    assert "' is paused for every project</b> by '" in js


def test_a_sign_in_shows_on_its_own_projects_page(client, install):
    from sellbird import inbox_routes
    from sellbird.workspace_home import use_home
    with use_home(projects.project_home(install, "alpha")):
        inbox_routes._job().update(state="waiting", kind="gmail", address="a@example.com")
    try:
        assert client.get("/api/inboxes/connect/status", headers={HEADER: "alpha"}).json()["state"] == "waiting"
        assert client.get("/api/inboxes/connect/status", headers={HEADER: "beta"}).json()["state"] == "idle"
    finally:
        inbox_routes._connect_jobs.clear()
