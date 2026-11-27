"""Each inbox signs as its own persona.

Drafts name their sender with {{sender_name}} / {{sender_first_name}}; the
Sender fills them in for the inbox each email actually leaves from, so the
body always matches the From header, whichever inbox the thread lands on.
"""

import asyncio
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from sellbird import mailboxes
from sellbird.email_style import fill_sender, with_sign_off
from sellbird.integrations.mail_provider import InboundMessage, SendResult
from sellbird.mailboxes import MAIN, main_sender_name, sender_name, with_senders
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.secrets import MemorySecretBackend, SecretStore

from tests.test_mailboxes import Box, add_inbox, boxes, store  # noqa: F401  (fixtures)
from tests.test_inbox_routes import _client, env as dash_env  # noqa: F401  (a fixture)
from tests.test_outbox_native import Cfg, Env, StubBrain, seed_prospect

SIDE_NAME = "Sam Rivers"
SIDE_SIGN_OFF = "سام ريفرز"


class Recording(Box):
    """A fake inbox that keeps the subject and body of what it sent."""

    def __init__(self, address, **kw):
        super().__init__(address, **kw)
        self.bodies = []

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **_):
        self.bodies.append((subject, body))
        return await super().send_email(to_email, subject, body, thread_ref, in_reply_to)


@pytest.fixture
def recording(boxes):  # noqa: F811
    """Extra inboxes record their bodies too."""
    boxes["two@side.co"] = Recording("two@side.co")
    return boxes


class SignedCfg(Cfg):
    """Cfg whose main mailbox signs with a name in another script."""

    class persona(Cfg.persona):
        sign_off_name = "أليكس مور"


def sender_for(state, main, cfg=None, *, approval=False):
    from sellbird.agents.sender import Sender
    cfg = cfg or Cfg()
    cfg.channels.email.require_approval = approval
    s = Sender(brain=None, state=state, config=cfg, env=Env())
    s.provider = main
    s.send_pacing = False
    return s


async def signed_campaign(state, prospect_ids):
    """A sequence as the native Writer leaves it: sender tags, not a name."""
    campaign = Campaign(
        id="", name="signed", channel="email", status="draft", prospect_ids=prospect_ids,
        sequence=[
            EmailStep(step=1, subject="for {{company}}", delay_days=0,
                      body="Hi {{first_name}}, I'm {{sender_name}}. Want a plan?\n\n{{sender_first_name}}"),
            EmailStep(step=2, subject="again", delay_days=3,
                      body="One more idea for {{company}}.\n\n{{ sender_first_name }}"),
        ])
    campaign.id = await state.add_campaign(campaign)
    return campaign


async def make_due(store, step):  # noqa: F811
    past = (datetime.now(timezone.utc) - timedelta(days=4)).replace(tzinfo=None).isoformat()
    async with store._connect() as db:
        await db.execute("UPDATE outbox SET send_at = ? WHERE step = ?", (past, step))
        await db.execute("UPDATE outbox SET sent_at = ? WHERE step = ?", (past, step - 1))
        await db.commit()


# ── the tags and the names ──

def test_fill_sender_puts_in_the_full_name_and_the_first_word():
    text = "I'm {{sender_name}}.\n\n{{ sender_first_name }}"
    assert fill_sender(text, "Sam  Rivers") == "I'm Sam Rivers.\n\nSam"
    assert fill_sender(text, SIDE_SIGN_OFF) == f"I'm {SIDE_SIGN_OFF}.\n\nسام"
    assert fill_sender("No tags here.", "Sam") == "No tags here."
    # No name at all: the tags go and nothing dangles.
    assert fill_sender("Thanks.\n\n{{sender_first_name}}", "") == "Thanks."


def test_the_sign_off_tag_replaces_a_name_the_model_wrote():
    body = "Want it?\n\nBest,\n{{sender_name}}"
    assert with_sign_off(body, "{{sender_first_name}}") == "Want it?\n\n{{sender_first_name}}"


def test_an_inbox_signs_as_its_sign_off_name_then_its_from_name_then_the_main_mailbox():
    assert main_sender_name(Cfg()) == "Sellbird"
    assert main_sender_name(SignedCfg()) == "أليكس مور"
    assert sender_name(None, Cfg()) == "Sellbird"
    assert sender_name({"display_name": SIDE_NAME}, Cfg()) == SIDE_NAME
    assert sender_name({"display_name": SIDE_NAME, "sign_off_name": SIDE_SIGN_OFF},
                       Cfg()) == SIDE_SIGN_OFF
    # An inbox with no name of its own is the main persona, in its script.
    assert sender_name({"display_name": ""}, SignedCfg()) == "أليكس مور"


def test_the_native_writer_signs_with_a_tag_and_instantly_keeps_the_name():
    from sellbird.agents.writer import Writer
    native = Writer(StubBrain(), None, Cfg())
    draft = {"subject": "site speed", "body": "Hi Jane,\n\nYour site is slow on phones. Want a fix?"}
    kept = asyncio.run(native._keep_to_rules(dict(draft), step=1))
    assert kept["body"].endswith("\n\n{{sender_first_name}}")
    assert "{{sender_name}}" in native._sender_rule()

    class InstantlyCfg(Cfg):
        class channels(Cfg.channels):
            class email(Cfg.channels.email):
                provider = "instantly"

    legacy = Writer(StubBrain(), None, InstantlyCfg())
    kept = asyncio.run(legacy._keep_to_rules(dict(draft), step=1))
    assert kept["body"].endswith("\n\nSellbird") and legacy._sender_rule() == ""


# ── sending ──

@pytest.mark.asyncio
async def test_each_inbox_signs_its_own_emails(store, recording):  # noqa: F811
    main = Recording("sellbird@x.co")
    side = await add_inbox(store, "two@side.co", display_name=SIDE_NAME)
    await store.set_sign_off_name(side, SIDE_SIGN_OFF)
    pids = [await seed_prospect(store, email=f"p{i}@acme{i}.com") for i in range(4)]
    await signed_campaign(store, pids)
    await sender_for(store, main)._run_native()

    assert len(main.bodies) == 2 and len(recording["two@side.co"].bodies) == 2
    for _, body in main.bodies:
        assert "I'm Sellbird." in body and body.split("\n\n")[1] == "Sellbird"
    for _, body in recording["two@side.co"].bodies:
        assert f"I'm {SIDE_SIGN_OFF}." in body and body.split("\n\n")[1] == "سام"
    # What was sent is what's stored: no tag left in any sent row.
    sent = [r for r in await store.get_outbox() if r["status"] == "sent"]
    assert sent and not any("{{" in r["subject"] + r["body"] for r in sent)


@pytest.mark.asyncio
async def test_the_main_mailbox_signs_with_its_configured_sign_off_name(store, recording):  # noqa: F811
    main = Recording("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")          # no name of its own
    pids = [await seed_prospect(store, email=f"p{i}@acme{i}.com") for i in range(2)]
    await signed_campaign(store, pids)
    await sender_for(store, main, SignedCfg())._run_native()
    assert main.bodies and recording["two@side.co"].bodies and side
    for box in (main, recording["two@side.co"]):
        for _, body in box.bodies:
            assert "I'm أليكس مور." in body and body.split("\n\n")[1] == "أليكس"


@pytest.mark.asyncio
async def test_a_follow_up_keeps_the_persona_of_its_thread(store, recording):  # noqa: F811
    main = Recording("sellbird@x.co")
    side = await add_inbox(store, "two@side.co", display_name=SIDE_NAME)
    pid = await seed_prospect(store)
    campaign = await signed_campaign(store, [pid])
    await store.set_campaign_pool(campaign.id, [side])
    sender = sender_for(store, main)
    await sender._run_native()
    await store.set_campaign_pool(campaign.id, [])        # the main mailbox may start threads now
    await make_due(store, 2)
    await sender._run_native()
    assert main.bodies == []
    first, follow_up = recording["two@side.co"].bodies
    assert first[1].split("\n\n")[1] == "Sam" and follow_up[1].split("\n\n")[1] == "Sam"


@pytest.mark.asyncio
async def test_a_first_email_that_moves_inbox_signs_as_the_new_one(store, recording):  # noqa: F811
    main = Recording("sellbird@x.co")
    side = await add_inbox(store, "two@side.co", display_name=SIDE_NAME)
    pid = await seed_prospect(store)
    campaign = await signed_campaign(store, [pid])
    await store.set_campaign_pool(campaign.id, [side])
    sender = sender_for(store, main, approval=True)
    await sender._run_native()                            # drafted for the side inbox
    pending = await with_senders(store, Cfg(), await store.get_outbox(status="pending_review"))
    step1 = next(r for r in pending if r["step"] == 1)
    assert step1["from_address"] == "two@side.co" and step1["from_name"] == SIDE_NAME
    assert step1["body"].endswith("\n\nSam") and step1["body_raw"].endswith("{{sender_first_name}}")

    await store.set_pause(side, "testing")
    await store.set_campaign_pool(campaign.id, [])
    async with store._connect() as db:
        await db.execute("UPDATE outbox SET status = 'approved'")
        await db.commit()
    await sender._run_native()
    assert recording["two@side.co"].bodies == []
    assert main.bodies[0][1].split("\n\n")[1] == "Sellbird" and "I'm Sellbird." in main.bodies[0][1]


@pytest.mark.asyncio
async def test_a_tag_that_cant_be_filled_is_never_sent(store, recording):  # noqa: F811
    main = Recording("sellbird@x.co")
    pid = await seed_prospect(store)
    campaign = Campaign(id="", name="typo", channel="email", status="draft", prospect_ids=[pid],
                        sequence=[EmailStep(step=1, subject="hello", delay_days=0,
                                            body="Want a plan?\n\n{{sender_nme}}")])
    await store.add_campaign(campaign)
    await sender_for(store, main)._run_native()
    assert main.bodies == []
    row = (await store.get_outbox())[0]
    assert row["status"] == "failed" and "merge tags" in row["error"]


@pytest.mark.asyncio
async def test_with_no_persona_set_an_email_reads_exactly_as_before(store, recording):  # noqa: F811
    main = Recording("sellbird@x.co")
    pid = await seed_prospect(store)
    await signed_campaign(store, [pid])
    sender = sender_for(store, main)
    await sender._run_native()
    (_, body), = main.bodies
    # The Writer used to sign with persona.name itself.
    before = with_sign_off("Hi Jane, I'm Sellbird. Want a plan?", "Sellbird")
    assert body == sender._with_footer(before)
    assert {r["mailbox_id"] for r in await store.get_outbox()} == {MAIN}


# ── replies ──

class Capturing(StubBrain):
    def __init__(self, prompt_file=""):
        super().__init__(intent="question", reply="It costs a little. Want the details?")
        self.prompts = []
        self.prompt_file = prompt_file

    def load_prompt(self, name, **_):
        return self.prompt_file if name == "handler" else ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.prompts.append((task, prompt))
        return await super().think(prompt, session_id, agent, task)


def _reply_prompt(brain):
    return next(p for task, p in brain.prompts if task == "generate_response")


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_file", ["", "You answer replies for X."])
async def test_a_reply_is_written_as_the_persona_of_the_inbox_it_arrived_in(
        store, recording, prompt_file):  # noqa: F811
    from sellbird.agents.handler import Handler
    main = Recording("sellbird@x.co")
    side = await add_inbox(store, "two@side.co", display_name=SIDE_NAME)
    await store.set_sign_off_name(side, SIDE_SIGN_OFF)
    await seed_prospect(store, status="contacted")
    brain = Capturing(prompt_file)
    handler = Handler(brain=brain, state=store, config=Cfg(), env=Env())
    handler.provider = main
    await handler._run_native()
    recording["two@side.co"].inbound = [InboundMessage(
        provider_id="r1", from_email="jane@acme.com", subject="hi",
        body="What does it cost?", message_id="<their@1>")]
    assert await handler._run_native() is True
    prompt = _reply_prompt(brain)
    assert f"You are {SIDE_SIGN_OFF}," in prompt and "You are Sellbird" not in prompt


@pytest.mark.asyncio
async def test_a_reply_on_the_main_mailbox_is_written_as_before(store, recording):  # noqa: F811
    from sellbird.agents.handler import Handler
    main = Recording("sellbird@x.co")
    await seed_prospect(store, status="contacted")
    brain = Capturing("You answer replies for X.")
    handler = Handler(brain=brain, state=store, config=Cfg(), env=Env())
    handler.provider = main
    main.inbound = [InboundMessage(provider_id="r1", from_email="jane@acme.com",
                                   subject="hi", body="What does it cost?", message_id="<m@1>")]
    assert await handler._run_native() is True
    # The prompt file as it was, with no persona line added.
    assert _reply_prompt(brain).startswith("You answer replies for X.\n\nConversation so far")


# ── setting the names ──

@pytest.mark.asyncio
async def test_the_cli_sets_an_inbox_sign_off_name(store, boxes, capsys):  # noqa: F811
    from sellbird import inbox_cli
    mid = await add_inbox(store, "two@side.co")
    secrets = SecretStore(backend=MemorySecretBackend(), env={})
    args = Namespace(inboxes_action="name", address="two@side.co", name=SIDE_NAME,
                     sign_off=SIDE_SIGN_OFF)
    assert await inbox_cli.run(args, store, secrets, Cfg(), Env()) == 0
    row = await store.get_mailbox(mid)
    assert row["display_name"] == SIDE_NAME and row["sign_off_name"] == SIDE_SIGN_OFF
    assert SIDE_SIGN_OFF in capsys.readouterr().out
    # The sign-off alone, then back to the From name.
    args = Namespace(inboxes_action="name", address="two@side.co", name=None, sign_off="")
    assert await inbox_cli.run(args, store, secrets, Cfg(), Env()) == 0
    row = await store.get_mailbox(mid)
    assert row["display_name"] == SIDE_NAME and sender_name(row, Cfg()) == SIDE_NAME


def test_the_cli_sets_the_main_mailbox_sign_off_name_in_the_config(tmp_path, capsys):
    from sellbird import inbox_cli
    from sellbird.config import load_config
    path = tmp_path / "sellbird.local.yaml"
    path.write_text(open("sellbird.yaml", encoding="utf-8").read(), encoding="utf-8")
    config = load_config(str(path))
    args = Namespace(inboxes_action="name", address="main", name=None, sign_off="أليكس مور")
    assert asyncio.run(inbox_cli.run(args, None, None, config, Env())) == 0
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["persona"]
    assert saved["sign_off_name"] == "أليكس مور" and saved["name"] == config.persona.name
    assert main_sender_name(load_config(str(path))) == "أليكس مور"


# ── the dashboard ──

def test_the_dashboard_sets_each_inbox_sign_off_name(dash_env, tmp_path, monkeypatch):
    import sellbird.dashboard as dash
    config_file = tmp_path / "sellbird.local.yaml"
    config_file.write_text(open("sellbird.yaml", encoding="utf-8").read(), encoding="utf-8")
    monkeypatch.setattr(dash, "_offer_config_file", lambda: config_file)
    mid = dash_env["mid"]
    with _client(dash_env["user"]) as c:
        assert c.post(f"/api/inboxes/{mid}/name", json={}).status_code == 400
        assert c.post(f"/api/inboxes/{mid}/name", json={"sign_off_name": "A\nB"}).status_code == 400
        assert c.post(f"/api/inboxes/{mid}/name", json={"sign_off_name": SIDE_SIGN_OFF}).json()["success"]
        assert c.post("/api/inboxes/main/name", json={"sign_off_name": "أليكس مور"}).json()["success"]
        assert c.post("/api/inboxes/main/name", json={"name": ""}).status_code == 400
        side = next(i for i in c.get("/api/inboxes").json()["inboxes"] if i["address"] == "two@side.co")
    row = asyncio.run(dash_env["store"].get_mailbox(mid))
    assert row["sign_off_name"] == SIDE_SIGN_OFF and row["display_name"] == ""
    assert side["signs_as"] == SIDE_SIGN_OFF and side["sign_off_name"] == SIDE_SIGN_OFF
    assert yaml.safe_load(config_file.read_text(encoding="utf-8"))["persona"]["sign_off_name"] == "أليكس مور"


def test_the_outbox_shows_who_a_draft_is_from_and_an_edit_keeps_the_tag(dash_env, monkeypatch):
    import sellbird.config
    monkeypatch.setattr(sellbird.config, "load_config", lambda *a, **k: Cfg())
    store, mid = dash_env["store"], dash_env["mid"]
    asyncio.run(store.set_sign_off_name(mid, SIDE_SIGN_OFF))
    item = asyncio.run(store.add_outbox_item(
        prospect_id="p1", to_email="jane@acme.com", subject="plan for acme", mailbox_id=mid,
        body="Want a plan?\n\n{{sender_first_name}}", send_at="2000-01-01"))
    with _client(dash_env["user"]) as c:
        draft = c.get("/api/outbox").json()["pending"][0]
        assert draft["from_address"] == "two@side.co"
        assert draft["body"] == "Want a plan?\n\nسام"
        assert draft["body_raw"] == "Want a plan?\n\n{{sender_first_name}}"
        edited = "Want the plan for Acme?\n\n{{sender_first_name}}"
        r = c.post(f"/api/outbox/{item}/edit", json={"subject": "plan for acme", "body": edited})
        assert r.json()["success"], r.json()
        assert c.post(f"/api/outbox/{item}/edit", json={
            "subject": "plan", "body": "Hi.\n\n{{sender_nme}}"}).status_code == 400
    assert asyncio.run(store.get_outbox_item(item))["body"] == edited
