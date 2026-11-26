"""What every project shares (sellbird.projects): ``<root>/data/shared.db``.

Opened only in projects mode, when the current home has a ``shared_dir``.
It holds three things:

- **suppressions**, the same table as a project's: an opt-out, bounce or
  complaint in one project stops every project;
- **company_claims**: which project is talking to a company. A project
  claims a company when it sends it a cold email; while another project's
  claim is active (last touched within ``company_claim_days``) that project's
  cold email waits and its Writer drafts nothing for the company. A reply
  renews the claim, and it lapses on its own;
- **inbox_sends**: one row per cold email, by inbox and project, so an
  inbox's ``inbox_daily_cap`` counts every project's sends;
- **settings** that are the install's, not a project's: the agent's
  on/off switch, since every project shares the agent's token.

A company is its domain as Sellbird recorded it; with none, the address
itself, so a free-mail address never claims every business on that provider.
"""

import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sellbird import storage
from sellbird.storage import BUSY_TIMEOUT_SECONDS

logger = logging.getLogger("sellbird.shared")

SHARED_DB = "shared.db"
# The Sender's hold note on a cold email another project's claim stops.
CLAIMED_REASON = "another project is in touch with this company"

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppressions (id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  value TEXT NOT NULL, reason TEXT DEFAULT 'manual', source TEXT DEFAULT '',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS uq_suppressions_kind_value ON suppressions(kind, value);
CREATE TABLE IF NOT EXISTS company_claims (domain TEXT PRIMARY KEY, project TEXT NOT NULL,
  claimed_at TEXT NOT NULL, last_touch_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox_sends (id INTEGER PRIMARY KEY AUTOINCREMENT,
  inbox TEXT NOT NULL, project TEXT NOT NULL, sent_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_inbox_sends_inbox ON inbox_sends(inbox, sent_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

_ready: set[str] = set()


def _utcnow() -> datetime:
    """Naive UTC now, as the outbox stores times. Tests pin it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(at: datetime) -> str:
    return at.replace(microsecond=0).isoformat()


class SharedState:
    def __init__(self, shared_dir: Path):
        self.db_path = str(Path(shared_dir) / SHARED_DB)

    @asynccontextmanager
    async def _connect(self):
        """A connection, with the schema created the first time this process
        opens the file (the dashboard may add a suppression before the loop
        ever ran)."""
        if self.db_path not in _ready:
            await self.init_db()
        async with storage.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS) as db:
            yield db

    async def init_db(self) -> None:
        # Done without awaiting, so tasks opening the file at once never
        # interleave here: switching a new file to WAL needs it to themselves.
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()
        _ready.add(self.db_path)

    # ── The install's settings ──

    async def get_setting(self, key: str) -> str | None:
        async with self._connect() as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._connect() as db:
            await db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            await db.commit()

    # ── Suppressions (keys normalised by StateManager.suppression_key) ──

    async def add_suppression(self, kind: str, key: str, reason: str, source: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO suppressions (id, kind, value, reason, source) VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, kind, key, reason, source[:200]))
            await db.commit()
            return cursor.rowcount > 0

    async def suppression_reasons(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        found: dict[tuple[str, str], str] = {}
        async with self._connect() as db:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                where = " OR ".join("(kind = ? AND value = ?)" for _ in chunk)
                params = [x for pair in chunk for x in pair]
                async with db.execute(f"SELECT kind, value, reason FROM suppressions WHERE {where}",
                                      params) as cursor:
                    for kind, value, reason in await cursor.fetchall():
                        found[(kind, value)] = reason
        return found

    async def list_suppressions(self, limit: int) -> list[dict]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT id, kind, value, reason, source, created_at FROM suppressions"
                " ORDER BY created_at DESC, id LIMIT ?", (int(limit),)) as cursor:
                cols = [c[0] for c in cursor.description]
                return [dict(zip(cols, r)) for r in await cursor.fetchall()]

    async def remove_suppression(self, suppression_id: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM suppressions WHERE id = ?", (suppression_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def lift_opt_out(self, address_key: str) -> None:
        async with self._connect() as db:
            await db.execute("DELETE FROM suppressions WHERE kind = 'address' AND value = ? "
                             "AND reason = 'opted_out'", (address_key,))
            await db.commit()

    # ── Company claims ──

    async def claim(self, domain: str, project: str, days: int,
                    at: datetime | None = None) -> str | None:
        """Claim ``domain`` for ``project``, or renew its claim. None when it
        is ``project``'s now; else the project holding it. One statement, so
        of two projects racing for a company exactly one wins."""
        now = at or _utcnow()
        lapsed = _iso(now - timedelta(days=days))
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO company_claims (domain, project, claimed_at, last_touch_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET
                     claimed_at = CASE WHEN company_claims.project = excluded.project
                                  THEN company_claims.claimed_at ELSE excluded.claimed_at END,
                     project = excluded.project,
                     last_touch_at = excluded.last_touch_at
                   WHERE company_claims.project = excluded.project
                      OR company_claims.last_touch_at < ?""",
                (domain, project, _iso(now), _iso(now), lapsed))
            await db.commit()
            async with db.execute("SELECT project FROM company_claims WHERE domain = ?",
                                  (domain,)) as cursor:
                row = await cursor.fetchone()
        holder = row[0] if row else project
        return None if holder == project else holder

    async def holder(self, domain: str, days: int, at: datetime | None = None) -> str | None:
        """The project with an active claim on ``domain``, or None."""
        lapsed = _iso((at or _utcnow()) - timedelta(days=days))
        async with self._connect() as db:
            async with db.execute(
                    "SELECT project FROM company_claims WHERE domain = ? AND last_touch_at >= ?",
                    (domain, lapsed)) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def touch(self, domain: str, project: str, at: datetime | None = None) -> None:
        """Renew ``project``'s own claim (a reply came in). Nobody else's."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE company_claims SET last_touch_at = ? WHERE domain = ? AND project = ?",
                (_iso(at or _utcnow()), domain, project))
            await db.commit()

    async def seed_claim(self, domain: str, project: str, at: datetime) -> None:
        """A claim from before projects mode (``projects init``); never
        replaces one already there."""
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO company_claims (domain, project, claimed_at, last_touch_at)"
                " VALUES (?, ?, ?, ?)", (domain, project, _iso(at), _iso(at)))
            await db.commit()

    async def release(self, domain: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM company_claims WHERE domain = ?", (domain,))
            await db.commit()
            return cursor.rowcount > 0

    async def list_claims(self) -> list[dict]:
        async with self._connect() as db:
            async with db.execute("SELECT domain, project, claimed_at, last_touch_at"
                                  " FROM company_claims ORDER BY domain") as cursor:
                cols = [c[0] for c in cursor.description]
                return [dict(zip(cols, r)) for r in await cursor.fetchall()]

    # ── Each inbox's cold email, every project together ──

    async def inbox_sent_since(self, inbox: str, since_iso: str) -> int:
        async with self._connect() as db:
            async with db.execute("SELECT COUNT(*) FROM inbox_sends WHERE inbox = ? AND sent_at >= ?",
                                  (inbox.lower(), since_iso)) as cursor:
                return (await cursor.fetchone())[0]

    async def reserve_inbox_send(self, inbox: str, project: str, cap: int,
                                 since_iso: str) -> int | None:
        """Record a cold email about to leave ``inbox``: its row id, or None
        when ``cap`` (0: none) is reached for every project together since
        ``since_iso``. One statement, so two projects can't both take the
        last place."""
        inbox = inbox.lower()
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT INTO inbox_sends (inbox, project, sent_at) SELECT ?, ?, ? WHERE ? = 0 OR "
                "(SELECT COUNT(*) FROM inbox_sends WHERE inbox = ? AND sent_at >= ?) < ?",
                (inbox, project, _iso(_utcnow()), int(cap), inbox, since_iso, int(cap)))
            await db.commit()
            return cursor.lastrowid if cursor.rowcount == 1 else None

    async def unreserve_inbox_send(self, row_id: int) -> None:
        """The email never left: give its place back."""
        async with self._connect() as db:
            await db.execute("DELETE FROM inbox_sends WHERE id = ?", (row_id,))
            await db.commit()


# ── The project's view, through its StateManager ──

def _settings(state):
    from sellbird.projects import live_settings
    return live_settings(Path(state.shared.db_path).parent.parent)


async def company_key(state, prospect) -> str:
    """The company a prospect belongs to, as claims count it."""
    company = await state.get_company(prospect.company_id) if prospect.company_id else None
    domain = state.suppression_key("domain", company.domain) if company and company.domain else ""
    return domain or (prospect.email or "").strip().lower()


async def claimed_elsewhere(state, prospect) -> str | None:
    """The other project in touch with this prospect's company, or None
    (always None outside projects mode)."""
    if state.shared is None:
        return None
    key = await company_key(state, prospect)
    if not key:
        return None
    holder = await state.shared.holder(key, _settings(state).company_claim_days)
    return holder if holder and holder != state.project else None


async def claim_company(state, prospect) -> str | None:
    """Claim (or renew) this prospect's company for this project, before a
    cold email. None when this project may send; else the project holding it."""
    if state.shared is None:
        return None
    key = await company_key(state, prospect)
    if not key:
        return None
    return await state.shared.claim(key, state.project, _settings(state).company_claim_days)


async def renew_claim(state, prospect) -> None:
    """A reply from the company keeps this project's claim on it."""
    if state.shared is None:
        return
    key = await company_key(state, prospect)
    if key:
        await state.shared.touch(key, state.project)
