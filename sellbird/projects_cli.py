"""`sellbird projects`: list, add, check and set up projects (docs/projects.md).

`init` turns a single install into its first project. It is a dry run unless
--apply is given; --apply first writes data/backup-<UTC time>.tar.gz of
everything it moves, and refuses while the loop runs.
"""

from __future__ import annotations

import asyncio
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
from sellbird.pidfile import loop_lock_held
from sellbird.shared import SharedState
from sellbird.state import StateManager
from sellbird.textio import read_user_text
from sellbird.workspace_home import Home, use_home

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
        running = "running" if loop_lock_held(home.data_dir / "sellbird.lock") else ""
        mark = "(default)" if name == default else ""
        print(f"  {name:<40} {_company(home.config_file) or '-':<30} {mark:<10} {running}")
    print()
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
    for name in names:
        with use_home(Home.for_project(root, name)):
            try:
                config = load_config()
            except Exception as e:
                problems.append(f"{name}: {e}")
                continue
        total += config.usage.max_daily_claude_percent
        if config.persona.company in UNTRAINED:
            warnings.append(f"{name}: not trained yet; `sellbird setup --project {name}`.")
    if total > 100:
        warnings.append(f"The projects' usage.max_daily_claude_percent add up to {total:g}%, "
                        "and they all spend one Claude account.")
    return problems, warnings


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
    extensions = sorted(p for p in root.glob("*.yaml") if p.name not in CONFIG_NAMES)
    data = root / "data"
    db_files = [data / f for f in DB_FILES if (data / f).exists()]
    moves = [(config, folder / "sellbird.yaml")]
    moves += [(p, folder / p.name) for p in extensions]
    moves += [(p, folder / "data" / p.name) for p in db_files]
    return {"folder": folder, "moves": moves, "has_db": bool(db_files)}


def _backup(root: Path, paths: list[Path]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / "data" / f"backup-{stamp}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wb") as gz, tarfile.TarFile(fileobj=gz, mode="w") as tar:
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
    finally:
        db.close()
    for domain, email, at in rows:
        key = StateManager.suppression_key("domain", domain) if domain else ""
        key = key or (email or "").strip().lower()
        if key and at and str(at) > out.get(key, ""):
            out[key] = str(at)
    return out


async def _seed_shared(root: Path, name: str, db_path: Path | None) -> tuple[int, int]:
    shared = SharedState(root / "data")
    await shared.init_db()
    if db_path is None:
        return 0, 0
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("SELECT id, kind, value, reason, source, created_at FROM suppressions").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        db.close()
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
    return copied, len(claims)


def cmd_init(root: Path, name: str, apply: bool) -> int:
    try:
        plan = init_plan(root, name)
    except (ValueError, projects.UnknownProject) as e:
        print(f"\n  {e}\n")
        return 1
    print(f"\n  {'Moving' if apply else 'Would move'} into {plan['folder']}:")
    for src, dst in plan["moves"]:
        print(f"    {src.relative_to(root)}  ->  {dst.relative_to(root)}")
    print("  Staying at the root, shared by every project: .env, the mailbox tokens,\n"
          "  the dashboard and agent tokens, prompts/ and skills/.")
    print(f"  {'Creating' if apply else 'Would create'} data/shared.db with this install's "
          "suppressions, and a claim on every company it has emailed.")
    if not apply:
        print("\n  Nothing changed. Run again with --apply to do it.\n")
        return 0
    if loop_lock_held(root / "data" / "sellbird.lock"):
        print("\n  Sellbird is running. Stop it first, then run this again.\n")
        return 1
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
    (plan["folder"] / "data").mkdir(parents=True, exist_ok=True)
    for src, dst in plan["moves"]:
        shutil.move(str(src), str(dst))
    moved_db = plan["folder"] / "data" / "sellbird.db" if plan["has_db"] else None
    copied, claimed = asyncio.run(_seed_shared(root, name, moved_db))
    print(f"  Shared: {copied} suppression(s) copied, {claimed} company claim(s) made.")
    with use_home(projects.project_home(root, name)):
        load_config()
    print(f"  Done. {name} is this install's first project; `sellbird run` runs it.\n")
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
    if action in ("new", "init") and not args.value:
        print(f"\n  Name the project: sellbird projects {action} NAME\n")
        sys.exit(2)
    try:
        if action == "new":
            code = cmd_new(root, args.value)
        elif action == "check":
            code = cmd_check(root)
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
    sub.set_defaults(func=cmd_projects)
