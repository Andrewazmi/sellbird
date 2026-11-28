"""Projects: several objectives in one install (docs/projects.md).

A project is a folder under ``<root>/projects/`` with its own ``sellbird.yaml``
and ``data/sellbird.db``. Projects mode is on when at least one such folder
exists; there is no list to keep in sync, and a folder without a
``sellbird.yaml`` is ignored. With no ``projects/`` folder nothing here runs
and the install works exactly as before.

``sellbird.projects.yaml`` at the root is optional and holds what spans
projects: the default project, the per-inbox cap across projects, how long a
first email holds a company, and a do-not-contact list added to every
project's own.
"""

import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from sellbird.workspace_home import Home, current_home, use_home

logger = logging.getLogger("sellbird.projects")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PROJECTS_DIR = "projects"
SETTINGS_FILE = "sellbird.projects.yaml"


class UnknownProject(ValueError):
    """Not a project name, or no such project under this root."""


def check_name(name: str) -> str:
    """The name, if it can be a project's folder name; else UnknownProject."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise UnknownProject(
            f"{name!r} isn't a project name: lower-case letters, digits and dashes, "
            "starting with a letter or digit, 40 characters at most.")
    return name


def discover(root: Path) -> list[str]:
    """Every project under ``root``, sorted: each folder in ``projects/``
    with a valid name and a ``sellbird.yaml`` in it."""
    folder = Path(root) / PROJECTS_DIR
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    return sorted(p.name for p in entries
                  if NAME_RE.match(p.name) and (p / "sellbird.yaml").is_file())


def enabled(root: Path) -> bool:
    """Projects mode: at least one project under ``root``."""
    return bool(discover(root))


def project_home(root: Path, name: str) -> Home:
    """The Home of an existing project. UnknownProject for anything else, so a
    typo never falls back to another project."""
    if check_name(name) not in discover(root):
        raise UnknownProject(f"No project named {name!r} in {Path(root) / PROJECTS_DIR}.")
    return Home.for_project(Path(root), name)


def install_root(home: Home | None = None) -> Path:
    """The install's root: the folder holding ``projects/``, whether the
    current home is a project or the root itself."""
    home = home or current_home()
    return home.shared_dir.parent if home.shared_dir is not None else home.root


# ── sellbird.projects.yaml ──

class ProjectsSettings(BaseModel):
    default: str = ""
    # Cold emails per inbox per day, all projects together. 0: no shared cap.
    inbox_daily_cap: int = Field(default=0, ge=0)
    # How long a first email holds a company for its project.
    company_claim_days: int = Field(default=60, ge=1)
    do_not_contact: list[str] = Field(default_factory=list)

    @field_validator("do_not_contact", mode="before")
    @classmethod
    def _dnc(cls, v):
        from sellbird.config import ComplianceConfig
        return ComplianceConfig(do_not_contact=v).do_not_contact


def load_settings(root: Path) -> ProjectsSettings:
    """``sellbird.projects.yaml``, or the defaults when there is none. Raises
    ConfigError on a file that can't be read or has a mistake in it."""
    from sellbird.config import ConfigError, _format_validation_error
    from sellbird.textio import read_user_text
    path = Path(root) / SETTINGS_FILE
    try:
        data = yaml.safe_load(read_user_text(path))
    except FileNotFoundError:
        return ProjectsSettings()
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"Could not read {path}: {e}")
    if data is None:
        return ProjectsSettings()
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping (key: value pairs).")
    try:
        return ProjectsSettings(**data)
    except ValidationError as e:
        raise ConfigError(f"Invalid settings in {path}:\n{_format_validation_error(e)}")


_last_good: dict[str, tuple[tuple, ProjectsSettings]] = {}


def live_settings(root: Path) -> ProjectsSettings:
    """``load_settings``, re-read when the file changes. A file that turns
    bad keeps the last good settings (a do-not-contact list must never be
    dropped by a typo), and says so."""
    path = Path(root) / SETTINGS_FILE
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        stamp = ()
    cached = _last_good.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        settings = load_settings(root)
    except Exception as e:
        if cached:
            logger.warning(f"{SETTINGS_FILE} couldn't be read ({e}); still using the last good one.")
            return cached[1]
        raise
    _last_good[str(path)] = (stamp, settings)
    return settings


# Not in v1: claims and the shared inbox count live in the native Sender.
INSTANTLY_REFUSED = ("sends through Instantly, which projects don't support yet. "
                     "Set channels.email.provider to gmail, microsoft or smtp in its sellbird.yaml.")


def config_problem(home: Home) -> str:
    """Why this project's config can't run in projects mode, or ""."""
    from sellbird.config import load_config
    with use_home(home):
        try:
            config = load_config()
        except Exception as e:
            return f"Project {home.project}: {e}"
    if getattr(config.channels.email, "provider", "") == "instantly":
        return f"Project {home.project} {INSTANTLY_REFUSED}"
    return ""


def default_project(root: Path) -> str | None:
    """The project the dashboard opens first: ``default`` when it names one,
    else the first by name. None outside projects mode."""
    names = discover(root)
    if not names:
        return None
    try:
        wanted = live_settings(root).default
    except Exception:
        wanted = ""
    return wanted if wanted in names else names[0]
