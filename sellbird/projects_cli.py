"""`sellbird projects`: list, add, check and set up projects (docs/projects.md).

`init` turns a single install into its first project. It is a dry run unless
--apply is given; --apply first writes data/backup-<UTC time>.tar.gz of
everything it moves, refuses while the loop or the dashboard runs, and puts
every file back if any step fails. `init --reseed` copies the projects'
suppressions, claims and agent switch into shared.db again.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sellbird import projects
from sellbird.config import ConfigError, load_config
from sellbird.pidfile import loop_lock_held, pid_alive
from sellbird.shared import SharedState
from sellbird.state import StateManager
from sellbird.textio import read_user_text
from sellbird.workspace_home import TRAINED_SKILLS, Home, use_home

CONFIG_NAMES = ("sellbird.yaml", "sellbird.local.yaml", projects.SETTINGS_FILE)
DB_FILES = ("sellbird.db", "sellbird.db-wal", "sellbird.db-shm")
UNTRAINED = ("", "Your Company")


def _company(path: Path) -> str:
    try:
        data = yaml.safe_load(read_user_text(path))
        return str(((data or {}).get("persona") or {}).get("company") or "")
    except Exception:
        return ""


def _template() -> str:
    """The untrained sellbird.yaml Sellbird ships."""
    from sellbird.home import bundled_defaults
    shipped = bundled_defaults()
    if shipped is not None and (shipped / "sellbird.yaml").is_file():
        text = read_user_text(shipped / "sellbird.yaml")
        if _company(shipped / "sellbird.yaml") in UNTRAINED:
            return text
    return "persona:\n  company: Your Company\n"


def cmd_list(root: Path) -> int:
    names = projects.discover(root)
    if not names:
        print("\n  No projects. This install is one objective, as always.\n"
              "  `sellbird projects init NAME` makes it the first project.\n")
        return 0
    default = projects.default_project(root)
    print()
    for name in names:
        home = Home.for_project(root, name)
        mark = "(default)" if name == default else ""
        print(f"  {name:<40} {_company(home.config_file) or '-':<30} {mark}")
    running = loop_lock_held(root / "data" / "sellbird.lock")
    print(f"\n  {'Running: one loop runs every project.' if running else 'Not running.'}\n")
    return 0


def cmd_new(root: Path, name: str) -> int:
    projects.check_name(name)
    folder = root / projects.PROJECTS_DIR / name
    if (folder / "sellbird.yaml").exists():
        print(f"\n  Project {name} already exists: {folder}\n")
        return 1
    (folder / "data").mkdir(parents=True, exist_ok=True)
    (folder / "sellbird.yaml").write_text(_template(), encoding="utf-8")
    print(f"\n  Created {folder / 'sellbird.yaml'}.\n"
          f"  Train it with `sellbird setup --project {name}`. Sellbird runs it from the next start.\n")
    return 0


def check(root: Path) -> tuple[list[str], list[str]]:
    """(problems, warnings) across every project."""
    problems, warnings = [], []
    try:
        settings = projects.load_settings(root)
    except ConfigError as e:
        return [str(e)], []
    names = projects.discover(root)
    if not names:
        return ["No projects under " + str(root / projects.PROJECTS_DIR) + "."], []
    if settings.default and settings.default not in names:
        warnings.append(f"default: {settings.default!r} isn't a project; the dashboard opens {names[0]}.")
    total = 0
    inboxes: dict[str, list[str]] = {}
    for name in names:
        home = Home.for_project(root, name)
        problem = projects.config_problem(home)
        if problem:
            problems.append(problem)
            continue
        with use_home(home):
            config = load_config()
        total += config.usage.max_daily_claude_percent
        if config.persona.company in UNTRAINED:
            warnings.append(f"{name}: not trained yet; `sellbird setup --project {name}`.")
        for address in _sending_addresses(config, home.db_path):
            inboxes.setdefault(address, []).append(name)
    if total > 100:
        warnings.append(f"The projects' usage.max_daily_claude_percent add up to {total:g}%, "
                        "and they all spend one Claude account.")
    shared_db = root / "data" / "shared.db"
    if shared_db.exists():
        pauses = asyncio.run(SharedState(root / "data").inbox_pauses())
        for inbox, pause in sorted(pauses.items()):
            gone = "" if pause["project"] in names else \
                " (that project no longer exists; `sellbird sending resume` lifts it)"
            warnings.append(f"{inbox} is paused for every project by {pause['project']}: "
                            f"{pause['reason']}{gone}.")
    if not settings.inbox_daily_cap:
        for address, users in sorted(inboxes.items()):
            if len(users) > 1:
                warnings.append(
                    f"{address} sends for {', '.join(users)}, and inbox_daily_cap is 0: each "
                    "project counts only its own email on it, so together they send more than "
                    "its warm-up allows. Set inbox_daily_cap in sellbird.projects.yaml.")
    return problems, warnings


def _sending_addresses(config, db_path: Path) -> set[str]:
    """The inboxes a project sends from: its main address and its extra inboxes."""
    from sellbird.integrations.mail_provider import identity_from_config
    found = {(identity_from_config(config).address or "").strip().lower()}
    if db_path.exists():
        db = sqlite3.connect(db_path)
        try:
            found |= {(r[0] or "").strip().lower()
                      for r in db.execute("SELECT address FROM mailboxes")}
        except sqlite3.Error:
            pass      # no mailboxes table yet
        finally:
            db.close()
    found.discard("")
    return found


def cmd_check(root: Path) -> int:
    problems, warnings = check(root)
    print()
    for line in problems:
        print(f"  ✗ {line}")
    for line in warnings:
        print(f"  ! {line}")
    if not problems and not warnings:
        print(f"  ✓ {len(projects.discover(root))} project(s), all fine.")
    print()
    return 1 if problems else 0


# ── init: a single install becomes the first project ──

def init_plan(root: Path, name: str) -> dict:
    """What `init` would move, or ValueError saying why it can't."""
    projects.check_name(name)
    if projects.discover(root):
        raise ValueError("This install already has projects. Add another with "
                         "`sellbird projects new NAME`.")
    folder = root / projects.PROJECTS_DIR / name
    if folder.exists():
        raise ValueError(f"{folder} already exists.")
    local, tracked = root / "sellbird.local.yaml", root / "sellbird.yaml"
    config = local if local.exists() else tracked
    if not config.exists() or _company(config) in UNTRAINED:
        raise ValueError("There is no trained config to move. Train this install first, "
                         "or start with `sellbird projects new NAME`.")
    # Extension settings sit beside the config as sellbird.<name>.yaml; any
    # other yaml at the root (a compose file, say) isn't Sellbird's.
    extensions = sorted(p for p in root.glob("sellbird.*.yaml") if p.name not in CONFIG_NAMES)
    skills = [root / "skills" / f"{s}.md" for s in TRAINED_SKILLS
              if (root / "skills" / f"{s}.md").is_file()]
    data = root / "data"
    db_files = [data / f for f in DB_FILES if (data / f).exists()]
    # The config goes last: a folder becomes a project once it has one.
    moves = [(p, folder / p.name) for p in extensions]
    moves += [(p, folder / "skills" / p.name) for p in skills]
    moves += [(p, folder / "data" / p.name) for p in db_files]
    moves += [(config, folder / "sellbird.yaml")]
    moving = {src for src, _ in moves}
    left = sorted(p for p in root.glob("*.yaml")
                  if p not in moving and p.name not in CONFIG_NAMES)
    return {"folder": folder, "moves": moves, "has_db": bool(db_files), "left": left}


def _backup(root: Path, paths: list[Path]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / "data" / f"backup-{stamp}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    # dereference: a symlinked config is backed up as its contents, not a link.
    with gzip.open(target, "wb") as gz, \
            tarfile.TarFile(fileobj=gz, mode="w", dereference=True) as tar:
        for p in paths:
            tar.add(p, arcname=str(p.relative_to(root)))
    return target


def _emailed(db_path: Path) -> dict[str, str]:
    """{company: last send} for every company the project has emailed, by
    the same key a claim uses (sellbird.shared.company_key)."""
    out: dict[str, str] = {}
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            """SELECT c.domain, p.email, MAX(o.sent_at) FROM outbox o
               JOIN prospects p ON p.id = o.prospect_id
               LEFT JOIN companies c ON c.id = p.company_id
               WHERE o.status = 'sent' AND o.kind = 'sequence' GROUP BY p.id""").fetchall()
        # Sent through Instantly, with no outbox row: the prospect's status says it.
        rows += db.execute(
            """SELECT c.domain, p.email, p.updated_at FROM prospects p
               LEFT JOIN companies c ON c.id = p.company_id
               WHERE p.status IN ('contacted', 'replied', 'meeting')
                 AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.prospect_id = p.id
                                 AND o.status = 'sent')""").fetchall()
    except sqlite3.OperationalError:
        rows = []       # a database from before the outbox
    finally:
        db.close()
    for domain, email, at in rows:
        key = StateManager.suppression_key("domain", domain) if domain else ""
        key = key or (email or "").strip().lower()
        if key and at and str(at) > out.get(key, ""):
            out[key] = str(at)
    return out


def _rows(db_path: Path, sql: str) -> list[tuple]:
    db = sqlite3.connect(db_path)
    try:
        return db.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


async def _seed_shared(root: Path, name: str, db_path: Path | None) -> tuple[int, int]:
    """Copy a project's suppressions, claims and agent switch into shared.db.
    Safe to repeat: nothing already there is replaced or doubled."""
    from sellbird.dashboard import AGENT_ACCESS_KEY
    shared = SharedState(root / "data")
    await shared.init_db()
    if db_path is None or not db_path.exists():
        return 0, 0
    rows = _rows(db_path, "SELECT id, kind, value, reason, source, created_at FROM suppressions")
    copied = 0
    async with shared._connect() as conn:
        for row in rows:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO suppressions (id, kind, value, reason, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)", row)
            copied += cursor.rowcount
        await conn.commit()
    claims = _emailed(db_path)
    for key, at in claims.items():
        await shared.seed_claim(key, name, datetime.fromisoformat(at[:19].replace(" ", "T")))
    # In projects mode the agent switch is the install's only: a project
    # that had cut the agent off keeps it off for every project.
    if ("off",) in _rows(db_path, f"SELECT value FROM settings WHERE key = '{AGENT_ACCESS_KEY}'"):
        await shared.set_setting(AGENT_ACCESS_KEY, "off")
    return copied, len(claims)


def _busy(root: Path) -> str:
    """Why init can't move files now, or ""."""
    from sellbird import dashboard_instance
    if loop_lock_held(root / "data" / "sellbird.lock"):
        return "Sellbird is running. Stop it first, then run this again."
    info = dashboard_instance.read(root / "data")
    if info is not None and pid_alive(info.pid):
        return "The dashboard is open. Stop `sellbird dashboard` first, then run this again."
    return ""


def _shared_files(root: Path) -> list[Path]:
    return [root / "data" / f"shared.db{s}" for s in ("", "-wal", "-shm")]


async def _drop_claims(root: Path, name: str) -> None:
    """Remove ``name``'s claims from a shared.db that was there before init
    (from a run that failed). No project by that name exists yet, so every
    claim it holds is one init seeded."""
    shared = SharedState(root / "data")
    async with shared._connect() as conn:
        await conn.execute("DELETE FROM company_claims WHERE project = ?", (name,))
        await conn.commit()


def _set_default(settings: Path, name: str) -> None:
    """Make the first project the dashboard's default, so a project added
    later (empty, maybe first in the alphabet) doesn't open instead. Never
    replaces a default already set; appends, so comments stay."""
    text = read_user_text(settings) if settings.exists() else ""
    if "default" in (yaml.safe_load(text) or {}):
        return
    line = f"default: {name}            # the dashboard opens this one first\n"
    settings.write_text(text + ("\n" if text and not text.endswith("\n") else "") + line,
                        encoding="utf-8")


def _apply(root: Path, name: str, plan: dict) -> tuple[int, int]:
    """Move everything into a temp folder, seed shared.db, then rename the
    folder into place. Any failure puts every file back."""
    folder = plan["folder"]
    staging = folder.parent / f".init-{name}"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "data").mkdir(parents=True)
    had_shared = [p for p in _shared_files(root) if p.exists()]
    settings = root / projects.SETTINGS_FILE
    settings_before = settings.read_bytes() if settings.exists() else None
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in plan["moves"]:
            tmp = staging / dst.relative_to(folder)
            tmp.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(tmp))
            done.append((src, tmp))
        db = staging / "data" / "sellbird.db" if plan["has_db"] else None
        seeded = asyncio.run(_seed_shared(root, name, db))
        load_config(str(staging / "sellbird.yaml"))    # refuse a config that won't load
        _set_default(settings, name)
        staging.rename(folder)
        return seeded
    except BaseException:
        for src, tmp in reversed(done):
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(src))
        shutil.rmtree(staging, ignore_errors=True)
        if had_shared:
            asyncio.run(_drop_claims(root, name))     # what this run seeded
        if settings_before is None:
            settings.unlink(missing_ok=True)
        else:
            settings.write_bytes(settings_before)
        with contextlib.suppress(OSError):
            folder.parent.rmdir()       # projects/, if init made it
        if not had_shared:
            for p in _shared_files(root):
                p.unlink(missing_ok=True)
        raise


def cmd_init(root: Path, name: str, apply: bool) -> int:
    try:
        plan = init_plan(root, name)
    except (ValueError, projects.UnknownProject) as e:
        print(f"\n  {e}\n")
        return 1
    if apply:
        busy = _busy(root)
        if busy:
            print(f"\n  {busy}\n")
            return 1
    print(f"\n  {'Moving' if apply else 'Would move'} into {plan['folder']}:")
    for src, dst in plan["moves"]:
        print(f"    {src.relative_to(root)}  ->  {dst.relative_to(root)}")
    print("  Staying at the root, shared by every project: .env, the mailbox tokens,\n"
          "  the dashboard and agent tokens, prompts/ and the shipped skills.")
    if plan["left"]:
        print(f"  {'Left' if apply else 'Would leave'} at the root: "
              f"{', '.join(p.name for p in plan['left'])}.\n"
              "  Only sellbird.<name>.yaml files move; an extension that keeps its settings\n"
              "  in another file may read it from the root.")
    print(f"  {'Creating' if apply else 'Would create'} data/shared.db with this install's "
          "suppressions, a claim on every company it has emailed, and its agent switch.")
    if not apply:
        print("\n  Nothing changed. Run again with --apply to do it.\n")
        return 0
    db = root / "data" / "sellbird.db"
    if db.exists():
        # Fold the write-ahead log into the database, so the files moved are whole.
        conn = sqlite3.connect(db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    backup = _backup(root, [src for src, _ in plan["moves"]])
    print(f"\n  Backup: {backup.relative_to(root)}")
    try:
        copied, claimed = _apply(root, name, plan)
    except Exception as e:
        print(f"  Couldn't finish, so every file is back where it was: {e}\n")
        return 1
    print(f"  Shared: {copied} suppression(s) copied, {claimed} company claim(s) made.")
    print(f"  Done. {name} is this install's first project; `sellbird run` runs it.\n")
    return 0


def cmd_reseed(root: Path) -> int:
    """Copy every project's suppressions, claims and agent switch into
    shared.db again (after a restore, say). Adds what's missing only."""
    names = projects.discover(root)
    if not names:
        print("\n  No projects to reseed from.\n")
        return 1
    busy = _busy(root)
    if busy:
        print(f"\n  {busy}\n")
        return 1
    print()
    for name in names:
        db = Home.for_project(root, name).db_path
        copied, claimed = asyncio.run(_seed_shared(root, name, db))
        print(f"  {name}: {copied} suppression(s) added, {claimed} company claim(s) checked.")
    print()
    return 0


def cmd_release(root: Path, value: str) -> int:
    key = value.strip().lower() if "@" in value else StateManager.suppression_key("domain", value)
    if not key:
        print(f"\n  That isn't a domain: {value}\n")
        return 1
    released = asyncio.run(SharedState(root / "data").release(key))
    print(f"\n  {'Released' if released else 'No project holds'} {key}.\n")
    return 0 if released else 1


def cmd_projects(args) -> None:
    from sellbird.paths import PROJECT_ROOT
    root = Path(PROJECT_ROOT)
    action = args.projects_action
    if action in ("new", "init") and not args.value and not getattr(args, "reseed", False):
        print(f"\n  Name the project: sellbird projects {action} NAME\n")
        sys.exit(2)
    try:
        if action == "new":
            code = cmd_new(root, args.value)
        elif action == "check":
            code = cmd_check(root)
        elif action == "init" and args.reseed:
            code = cmd_reseed(root)
        elif action == "init":
            code = cmd_init(root, args.value, args.apply)
        elif action == "release":
            if not args.value:
                print("\n  Name the company's domain: sellbird projects release DOMAIN\n")
                sys.exit(2)
            code = cmd_release(root, args.value)
        else:
            code = cmd_list(root)
    except projects.UnknownProject as e:
        print(f"\n  {e}\n")
        code = 1
    if code:
        sys.exit(code)


def add_parser(subparsers) -> None:
    sub = subparsers.add_parser("projects", help="Several objectives in one install")
    sub.add_argument("projects_action", nargs="?", default="list",
                     choices=["list", "new", "check", "init", "release"])
    sub.add_argument("value", nargs="?", default="",
                     help="new / init: the project's name; release: a domain")
    sub.add_argument("--apply", action="store_true",
                     help="init: do it (without this, init only says what it would do)")
    sub.add_argument("--reseed", action="store_true",
                     help="init: copy every project's suppressions, claims and agent switch "
                          "into shared.db again; adds what's missing only")
    sub.set_defaults(func=cmd_projects)
