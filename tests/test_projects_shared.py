"""Projects' shared layer (sellbird.shared): an opt-out in one project stops
the others, a company belongs to one project at a time, and an inbox's daily
cap counts every project's cold email. Fake mail providers only."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sellbird import projects
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.shared import SharedState, claimed_elsewhere
from sellbird.state import CLAIM_HOLD_PREFIX, StateManager
from sellbird.workspace_home import Home, use_home
from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _fresh_settings():
    projects._last_good.clear()
    yield
    projects._last_good.clear()


class Project:
    """One project's home, database and Sender, with its own fake provider."""

    def __init__(self, root, name):
        self.home = Home.for_project(root, name)
        self.home.root.mkdir(parents=True, exist_ok=True)
        (self.home.root / "sellbird.yaml").write_text("persona:\n  company: Example Co\n",
                                                     encoding="utf-8")
        with use_home(self.home):
            self.state = StateManager()
        self.provider = FakeProvider()

    async def prospect(self, email="pat@acme.example", domain="acme.example"):
        with use_home(self.home):
            await self.state.init_db()
            company_id = await self.state.add_company(Company(name="Acme", domain=domain))
            pid = await self.state.add_prospect(Prospect(
                first_name="Pat", last_name="Doe", title="Owner", company="Acme",
                company_id=company_id, email=email, email_status="verified",
                email_verified=True, status="new"))
            await seed_campaign(self.state, [pid])
            return pid

    async def send(self):
        """Stage approved and drain, inside this project's home."""
        with use_home(self.home):
            sender = make_sender(self.state, self.provider, require_approval=False)
            await sender._run_native()


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.mark.asyncio
async def test_the_shared_layer_is_only_there_in_projects_mode(tmp_path):
    with use_home(Home.for_root(tmp_path)):
        assert StateManager().shared is None and StateManager().project is None
    assert StateManager(str(tmp_path / "x.db")).shared is None
    with use_home(Home.for_project(tmp_path, "product-a")):
        s = StateManager()
    assert s.project == "product-a"
    assert s.shared.db_path == str(tmp_path / "data" / "shared.db")


@pytest.mark.asyncio
async def test_an_opt_out_in_one_project_stops_the_other(root):
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    await b.prospect()
    with use_home(b.home):   # B's first email waits for approval
        await make_sender(b.state, b.provider, require_approval=True)._run_native()
    with use_home(a.home):
        await a.state.init_db()
        assert await a.state.add_suppression("address", "Pat@Acme.example", "opted_out")
        # Written to the shared table, not the project's own.
        assert await a.state._own_suppressions(10, 0) == []
    assert await b.state.is_suppressed("pat@acme.example") == "opted_out"
    assert [r["value"] for r in await b.state.list_suppressions()] == ["pat@acme.example"]

    for item in await b.state.get_outbox(status="pending_review"):
        await b.state.update_outbox_item(item["id"], status="approved", approved_by="you")
    await b.send()
    assert b.provider.sent == []
    cancelled = await b.state.get_outbox(status="cancelled")
    assert cancelled and cancelled[0]["error"] == "suppressed (opted_out)"


@pytest.mark.asyncio
async def test_removing_a_shared_suppression(root):
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    with use_home(a.home):
        await a.state.init_db()
        await a.state.add_suppression("domain", "acme.example", "manual")
    with use_home(b.home):
        await b.state.init_db()
        [row] = await b.state.list_suppressions()
        assert await b.state.remove_suppression(row["id"])
    assert await a.state.is_suppressed("pat@acme.example") is None


@pytest.mark.asyncio
async def test_the_shared_do_not_contact_list_covers_every_project(root):
    (root / "sellbird.projects.yaml").write_text("do_not_contact: [acme.example]\n", encoding="utf-8")
    b = Project(root, "segment-b")
    await b.prospect()
    await b.send()
    assert b.provider.sent == []
    [item] = await b.state.get_outbox(status="failed")
    assert "do-not-contact" in item["error"] or "do_not_contact" in item["error"]


@pytest.mark.asyncio
async def test_a_claim_by_one_project_holds_the_others_first_email(root):
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    await a.prospect(email="pat@acme.example")
    pid_b = await b.prospect(email="sam@acme.example")   # same company, another person

    await a.send()
    assert [m["to"] for m in a.provider.sent] == ["pat@acme.example"]
    shared = SharedState(root / "data")
    assert [c["project"] for c in await shared.list_claims()] == ["product-a"]

    # B's Writer skips the company, and its Sender holds the email.
    with use_home(b.home):
        assert await claimed_elsewhere(b.state, await b.state.get_prospect(pid_b)) == "product-a"
    await b.send()
    assert b.provider.sent == []
    held = [i for i in await b.state.get_outbox(status="approved") if i["step"] == 1]
    assert held and held[0]["error"].startswith(CLAIM_HOLD_PREFIX)
    assert "product-a" in held[0]["error"]
    assert held[0]["send_at"] > _utcnow().isoformat()

    # Released, B may write and send again (the hold moved the email back a day).
    assert await shared.release("acme.example")
    with use_home(b.home):
        assert await claimed_elsewhere(b.state, await b.state.get_prospect(pid_b)) is None
        await b.state.update_outbox_item(held[0]["id"], send_at=_utcnow().isoformat())
    await b.send()
    assert [m["to"] for m in b.provider.sent] == ["sam@acme.example"]
    sent = await b.state.get_outbox(status="sent")
    assert sent[0]["error"] == ""      # the hold note went with it
    assert [c["project"] for c in await shared.list_claims()] == ["segment-b"]


@pytest.mark.asyncio
async def test_two_projects_racing_for_a_company_have_one_winner(tmp_path):
    shared = SharedState(tmp_path)
    results = await asyncio.gather(*[shared.claim("acme.example", name, 60)
                                     for name in ("product-a", "segment-b", "third-c")])
    assert results.count(None) == 1
    winner = (await shared.list_claims())[0]["project"]
    assert all(r in (None, winner) for r in results)


@pytest.mark.asyncio
async def test_a_claim_lapses_and_a_reply_renews_it(tmp_path):
    shared = SharedState(tmp_path)
    start = datetime(2026, 1, 1)
    assert await shared.claim("acme.example", "product-a", 60, at=start) is None
    # Within the window, the other project is turned away; its own renews.
    assert await shared.claim("acme.example", "segment-b", 60, at=start + timedelta(days=59)) == "product-a"
    await shared.touch("acme.example", "product-a", at=start + timedelta(days=50))
    assert await shared.holder("acme.example", 60, at=start + timedelta(days=100)) == "product-a"
    # Someone else's touch never renews it.
    await shared.touch("acme.example", "segment-b", at=start + timedelta(days=200))
    assert await shared.holder("acme.example", 60, at=start + timedelta(days=111)) is None
    assert await shared.claim("acme.example", "segment-b", 60, at=start + timedelta(days=111)) is None
    [claim] = await shared.list_claims()
    assert claim["project"] == "segment-b" and claim["claimed_at"].startswith("2026-04-22")


@pytest.mark.asyncio
async def test_a_free_mail_address_claims_only_itself(root):
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    await a.prospect(email="pat@mail.example", domain="")
    pid_b = await b.prospect(email="sam@mail.example", domain="")
    await a.send()
    with use_home(b.home):
        assert await claimed_elsewhere(b.state, await b.state.get_prospect(pid_b)) is None
    await b.send()
    assert [m["to"] for m in b.provider.sent] == ["sam@mail.example"]


@pytest.mark.asyncio
async def test_the_inbox_cap_counts_every_project(root):
    (root / "sellbird.projects.yaml").write_text("inbox_daily_cap: 1\n", encoding="utf-8")
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    await a.prospect(email="pat@acme.example", domain="acme.example")
    await b.prospect(email="sam@other.example", domain="other.example")
    await a.send()
    assert len(a.provider.sent) == 1
    await b.send()
    assert b.provider.sent == []     # same inbox (one persona address), its day is used
    held = [i for i in await b.state.get_outbox(status="approved") if i["step"] == 1]
    assert held and held[0]["error"] == ""


@pytest.mark.asyncio
async def test_no_inbox_cap_still_records_each_send(root):
    a = Project(root, "product-a")
    await a.prospect()
    await a.send()
    assert len(a.provider.sent) == 1
    assert await SharedState(root / "data").inbox_sent_since("SELLBIRD@x.co", "2000-01-01") == 1


@pytest.mark.asyncio
async def test_a_failed_send_gives_its_place_back(root):
    (root / "sellbird.projects.yaml").write_text("inbox_daily_cap: 1\n", encoding="utf-8")
    a = Project(root, "product-a")
    await a.prospect()
    a.provider.fail_next = True
    await a.send()
    assert await SharedState(root / "data").inbox_sent_since("sellbird@x.co", "2000-01-01") == 0


@pytest.mark.asyncio
async def test_reserving_the_last_place_has_one_winner(tmp_path):
    shared = SharedState(tmp_path)
    got = await asyncio.gather(*[shared.reserve_inbox_send("me@example.com", f"p{i}", 2, "2000-01-01")
                                 for i in range(6)])
    assert sum(g is not None for g in got) == 2


@pytest.mark.asyncio
async def test_the_inbox_cap_holds_with_the_cold_email_check_on(root):
    """The Precleared check names the recipient: the ledger still counts the
    sending inbox (review B1)."""
    from tests.test_precleared import OK_BODY, mock_status, with_check
    (root / "sellbird.projects.yaml").write_text("inbox_daily_cap: 1\n", encoding="utf-8")
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    await a.prospect(email="pat@acme.example", domain="acme.example")
    await b.prospect(email="sam@other.example", domain="other.example")
    for p in (a, b):
        with use_home(p.home):
            s = with_check(make_sender(p.state, p.provider, require_approval=False),
                           mock_status(200, OK_BODY))
            await s._run_native()
    assert len(a.provider.sent) + len(b.provider.sent) == 1
    shared = SharedState(root / "data")
    assert await shared.inbox_sent_since("sellbird@x.co", "2000-01-01") == 1
    assert await shared.inbox_sent_since("pat@acme.example", "2000-01-01") == 0


@pytest.mark.asyncio
async def test_a_send_that_fails_gives_a_fresh_claim_back(root):
    """A claim is taken as the email leaves; nothing left, nothing held (review I7)."""
    a = Project(root, "product-a")
    await a.prospect()
    a.provider.fail_next = True
    await a.send()
    assert await SharedState(root / "data").list_claims() == []


@pytest.mark.asyncio
async def test_a_held_email_claims_nothing(root):
    """An email the shared cap holds never claims its company."""
    (root / "sellbird.projects.yaml").write_text("inbox_daily_cap: 1\n", encoding="utf-8")
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    await a.prospect(email="pat@acme.example", domain="acme.example")
    await b.prospect(email="sam@other.example", domain="other.example")
    await a.send()
    await b.send()
    claims = await SharedState(root / "data").list_claims()
    assert [(c["domain"], c["project"]) for c in claims] == [("acme.example", "product-a")]


@pytest.mark.asyncio
async def test_a_renewed_claim_stays_when_a_send_fails(root):
    a = Project(root, "product-a")
    await a.prospect()
    shared = SharedState(root / "data")
    await shared.claim("acme.example", "product-a", 60)
    a.provider.fail_next = True
    await a.send()
    assert [c["project"] for c in await shared.list_claims()] == ["product-a"]


@pytest.mark.asyncio
async def test_get_replies_reads_without_marking_in_projects_mode(tmp_path, monkeypatch):
    """Through get_replies, the Handler's path: the thread it reads in keeps
    the project's home (review B2)."""
    import imaplib
    from types import SimpleNamespace
    from sellbird.integrations.smtp_mail import SmtpImapProvider
    fetched = []

    class FakeImap:
        def __init__(self, *a): pass
        def login(self, *a): pass
        def select(self, *a): pass
        def search(self, *a): return "OK", [b"1"]
        def fetch(self, msg_id, what):
            fetched.append(what)
            return "NO", []
        def logout(self): pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    env = SimpleNamespace(smtp_host="smtp.example.com", smtp_port=587, smtp_username="u",
                          smtp_password="p", imap_host="imap.example.com", imap_port=993,
                          imap_username="u", imap_password="p")
    provider = SmtpImapProvider(SimpleNamespace(persona=None), env)
    await provider.get_replies(10)
    with use_home(Home.for_project(tmp_path, "product-a")):
        await provider.get_replies(10)
    assert fetched == ["(RFC822)", "(BODY.PEEK[])"]


def test_imap_reads_without_marking_in_projects_mode(tmp_path, monkeypatch):
    """Two projects read one inbox: the first must not hide a reply from the
    second by marking it seen."""
    import imaplib
    from types import SimpleNamespace
    from sellbird.integrations.smtp_mail import SmtpImapProvider

    fetched = []

    class FakeImap:
        def __init__(self, *a):
            pass

        def login(self, *a):
            pass

        def select(self, *a):
            pass

        def search(self, *a):
            return "OK", [b"1"]

        def fetch(self, msg_id, what):
            fetched.append(what)
            return "NO", []

        def logout(self):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    env = SimpleNamespace(smtp_host="smtp.example.com", smtp_port=587, smtp_username="u",
                          smtp_password="p", imap_host="imap.example.com", imap_port=993,
                          imap_username="u", imap_password="p")
    provider = SmtpImapProvider(SimpleNamespace(persona=None), env)
    provider._fetch_unseen(10)
    with use_home(Home.for_project(tmp_path, "product-a")):
        provider._fetch_unseen(10)
    assert fetched == ["(RFC822)", "(BODY.PEEK[])"]


@pytest.mark.asyncio
async def test_a_bounce_rate_trip_pauses_the_inbox_for_every_project(root, monkeypatch, capsys):
    """The inbox's reputation is every project's (review I4a): one project's
    bounce rate pausing it holds the others' email from it, until the
    project that tripped it resumes sending."""
    from types import SimpleNamespace
    from sellbird import cli
    from sellbird.agents import handler as handler_mod
    from tests.test_outbox_native import make_handler
    monkeypatch.setattr(handler_mod, "MIN_SENDS_FOR_KILL_SWITCH", 1)
    a, b = Project(root, "product-a"), Project(root, "segment-b")
    await a.prospect(email="pat@acme.example", domain="acme.example")
    await b.prospect(email="sam@other.example", domain="other.example")
    await a.send()
    [sent] = await a.state.get_outbox(status="sent")

    with use_home(a.home):
        bounce = SimpleNamespace(in_reply_to=sent["message_id"], subject="Undeliverable")
        await make_handler(a.state, a.provider)._handle_bounce(bounce)
        assert await a.state.get_setting("sending_paused")
    shared = SharedState(root / "data")
    assert (await shared.inbox_pauses())["sellbird@x.co"]["project"] == "product-a"

    await b.send()
    assert b.provider.sent == []        # same inbox: held, not lost
    assert 1 in [i["step"] for i in await b.state.get_outbox(status="approved")]

    # Resuming segment-b doesn't lift product-a's pause; resuming product-a does.
    with use_home(b.home):
        await asyncio.to_thread(cli.cmd_sending, SimpleNamespace(sending_action="resume"))
    assert await shared.inbox_pauses()
    with use_home(a.home):
        await asyncio.to_thread(cli.cmd_sending, SimpleNamespace(sending_action="resume"))
    assert "1 inbox pause(s) lifted" in capsys.readouterr().out
    assert await shared.inbox_pauses() == {}
    await b.send()
    assert [m["to"] for m in b.provider.sent] == ["sam@other.example"]
