# Projects: several objectives in one install

Status: implemented. Nothing here changes an install that has no `projects/`
folder.

## The problem

One install is one objective today. `sellbird.yaml` (or `sellbird.local.yaml`)
holds the ICP, the product, the offer and the writer's email rules, and
`data/sellbird.db` holds the companies, prospects and campaigns found for it.
Someone selling several products, or one product to several segments, runs a
checkout per objective: a copy of the code, a virtualenv, a dashboard and a loop
each, and nothing stops two of them from emailing the same company in the
same week.

A project is one objective inside one install. Each project has its own
settings and database. The code, the virtualenv, the API keys, the mailboxes
and the dashboard are shared, and a few safety rules work across all
projects.

| Per project | Shared by every project |
|---|---|
| ICP, product, offer (`sellbird.yaml`) | the code and the Python environment |
| what training learned (`skills/product_knowledge.md`, `competitive_intel.md`) | `.env` (API keys, brain login) |
| `writer.email_rules`, prompts and skills overrides | the agent's on/off switch |
| companies, prospects, campaigns, Outbox (`data/sellbird.db`) | the mailboxes and their tokens |
| daily send cap, sending mode, quiet hours | one dashboard, with a project switcher |
| extension settings beside its `sellbird.yaml` | suppressions (opt-outs, bounces, do-not-contact) |
| | which project is talking to a company right now |
| | each inbox's daily cap across projects |

## What already exists

Most of the groundwork is in `sellbird/workspace_home.py`. A `Home` is every
path one workspace needs, and the current home lives in a ContextVar, so a task
started inside `use_home(home)` keeps that home and `asyncio.to_thread` inherits
it. `config.load_config()` reads `current_home().config_file`, a bare
`StateManager()` opens `current_home().db_path`, and the brain, the writer, the
secrets store and the mailbox token files go through `current_home()` too.
`config.use_config()` does the same for an already-loaded config, and the
dashboard's `home_middleware` runs a request in `app.state.home`.

What's missing for self-hosted use:

- a way to declare several homes under one root;
- a loop that runs each of them;
- the dashboard still reads its module-level `DB_PATH` in a dozen places, and
  it has no way to pick a home per request;
- a layer shared by every project for suppressions, company claims and inbox
  caps.

## Layout

```text
<root>/                       the checkout or SELLBIRD_HOME, as today
  .env                        shared: API keys, brain settings
  sellbird.projects.yaml      optional: settings that span projects
  prompts/  skills/           shared defaults
  data/
    shared.db                 suppressions, company claims, inbox ledger and
                              pauses, the agent's switch
    dashboard-token           one dashboard login
    agent-token
    m365_token.json           mailbox tokens, shared
    gmail_token.json
  projects/
    product-a/
      sellbird.yaml           this project's ICP, product, offer, email_rules, caps
      data/sellbird.db        this project's companies, prospects, campaigns
      skills/                 what training wrote about this project's product
      prompts/  skills/       optional: other files here win over the shared ones
    segment-b/
      sellbird.yaml
      data/sellbird.db
```

Projects mode is on when `<root>/projects/` holds at least one folder with a
`sellbird.yaml` in it. A project's name is its folder name, which must match
`^[a-z0-9][a-z0-9-]{0,39}$`. There is no list to keep in sync: adding a
folder adds a project, and a folder without a `sellbird.yaml` is ignored.
`projects/` and `sellbird.projects.yaml` are gitignored, like
`sellbird.local.yaml`: they hold trained settings and databases.

`sellbird.projects.yaml` is optional and small:

```yaml
default: product-a            # the dashboard opens this one first
inbox_daily_cap: 40           # cold emails per inbox per day, all projects together
company_claim_days: 60        # how long a first email holds a company for its project
do_not_contact:               # added to every project's own list
  - competitor.example
```

A project's `Home` comes from a new `Home.for_project(root, name)`:

- `root`, `data_dir`, `db_path` and `config_file` are the project folder's;
- `env_file`, the mailbox token files, `dashboard_token_file` and
  `agent_token_file` are the root's, so keys, mailboxes and the dashboard
  login are shared;
- a new `shared_dir` field points at `<root>/data`. It is `None` outside
  projects mode, which is how the code knows the shared layer is off;
- a new `project` field holds the name, or `None`.

Prompts and skills resolve per file: the project's copy, then the root's, then
the copy shipped with Sellbird. `brain._read_knowledge` already falls back to
the shipped copy, and it gains the root step (`Home.knowledge_dirs`). Social
playbooks, a relative `writer.email_rules` path and the pre-send gate's check
for quoted instructions resolve the same way.

The two files training writes, `skills/product_knowledge.md` and
`skills/competitive_intel.md` (`workspace_home.TRAINED_SKILLS`), are the
exception: a project reads only its own. Before `projects init` the root's
copies describe the first product, so a second project falling back to them
would sell the wrong thing. An untrained project reads them as empty, and
`init` moves the root's copies into the first project. `projects new` starts
without them.

Extensions need no change. An extension that keeps its settings beside the
`sellbird.yaml` in use, named `sellbird.<name>.yaml`, reads the project's own
file, and `init` moves those files with the config. Tick steps already get
the cycle's config and state, which are the project's. `extensions.registry()`
is process-wide, not per project: an extension is installed once and its
routes, tick steps and commands serve every project, so a tick step must read
the project from the context it is given, never from module state.

## The loop

`sellbird run` in projects mode starts one asyncio task per project. Each task
is created inside `use_home(project_home)`, so the context copy it gets holds
that home, and runs the existing `heartbeat()` unchanged: it loads its own
config, opens its own database and builds its own agents. Stopping the
process stops every task through the existing stop event.

There is one runner, and it holds the root's `data/sellbird.lock` and writes
the root's `data/sellbird.pid`, the files the dashboard already reads to tell
whether the loop is running; `pidfile.py` is unchanged. A second `sellbird
run` refuses while the first holds the lock. At start the runner skips, with
a line saying why, a project that isn't set up or whose config fails to
load, and runs the rest; with none left it exits 1.

Code that leaves the event loop for a thread uses `asyncio.to_thread`, which
copies the context and so the home. `loop.run_in_executor` does not, and
would read the default project's files from the thread (IMAP, the email
finder's lookups, the quota reader and Scout all use `to_thread`).

Projects send through Gmail, Microsoft 365 or SMTP. Instantly is not
supported in projects mode in this version: company claims, the shared inbox
cap and the inbox pauses live in the native Sender's outbox, which Instantly
campaigns bypass. The runner skips a project set to Instantly, and `sellbird
projects check` reports it as a config error.

Log lines name their project (`[sellbird.sender:product-a]`), read from the
task's home, so threads it starts are named too.

Each project keeps its own `usage.max_daily_claude_percent`, and they all
spend one Claude account. `sellbird projects check` warns when they add up
to more than 100.

## The command line

`sellbird --project NAME <command>`, or `SELLBIRD_PROJECT=NAME`, runs any
command inside that project's home: `outbox`, `status`, `export`, `import`,
`signals`, `discover` and the rest read and write that project's database
and config. `setup`, `train` and `mcp` also take `--project` after the
command. An unknown name lists the projects and exits 1, and `--project` on
an install without projects is refused, so a typo never acts on the wrong
data.

In projects mode a command that reads a project's data, given no project,
is refused with the list of projects (exit 2), rather than opening the
root's database, which no project reads. Two commands act on the whole
install instead:

- `sellbird sending pause|resume|status` with no project acts on every
  project in turn (pausing is an emergency stop); with `--project` it acts
  on that one. Resuming a project also lifts the inbox pauses its bounce
  rate set (see below).
- `sellbird suppressions add|remove|list` edits the shared list in
  `shared.db`, which every project checks.

Commands that touch no project data (`run`, `dashboard`, `install`, `gmail`,
`microsoft`, `connect`, `autostart`, `tray`, `recommended`, `precleared-key`,
`projects`) run as before.

## The dashboard

One dashboard process serves every project, with one token (the root's), so
signing in once covers all projects.

- `GET /api/projects` lists the projects and which one is the default. Outside
  projects mode it returns an empty list and the UI shows nothing new.
- The UI sends `X-Sellbird-Project: <name>` on every API call, taken from the
  switcher in the header. A request without it uses the `sellbird_project`
  cookie that the switcher sets, then the default project. Using a header means
  two tabs on two projects can't act on each other's data.
- `home_middleware` turns that into `use_home(Home.for_project(root, name))`
  for the request. An unknown name gets a 404, never a fallback, so a typo
  never writes into the wrong project.
- Every module-level `DB_PATH` read in `dashboard.py` goes through
  `current_home().db_path`, which outside projects mode is the same file.
- Starting and stopping the loop from the dashboard controls the one process,
  and the status says that one loop runs every project.
  Pausing sending is the project's own (`sending_paused` in its database), so
  with projects the button reads "Pause sending for this project".
- The Inboxes page reads and writes the selected project's own inboxes, and
  a sign-in in progress shows on its own project's page. Both sign-in flows
  listen on one local port, so one runs at a time.
- The agent's on/off switch is the install's, since every project shares the
  agent's token: in projects mode it lives in `shared.db` alone. `init`
  carries a switch that was off across.
- The MCP server sends the same header. `sellbird mcp --project NAME` pins
  it to one project.

## Mailboxes and caps

Mailboxes are shared: the tokens live at the root, and each project's
`sellbird.yaml` lists the inboxes it sends from, as it does today.

There are two caps:

1. **Per project, per inbox:** the project's own `email.max_daily_sends`,
   `per_inbox_daily_cap` and warm-up ramp, exactly as today.
2. **Per inbox, across projects:** `inbox_daily_cap` in
   `sellbird.projects.yaml`. In projects mode the Sender writes one row per cold
   send to `shared.db`'s `inbox_sends(inbox, project, sent_at)`, the inbox being
   its sending address. Before sending, a project's room on an inbox is the
   smaller of its own remaining cap and `inbox_daily_cap` minus today's rows
   (from local midnight, as the cold caps count) for that inbox from every
   project. The row is taken just before the send in one conditional insert,
   so two projects can't both take the last place, and given back when the
   provider says the email didn't go. The row records the sending inbox,
   never the recipient, also when the cold-email check is on.

`sellbird projects check` warns when two or more projects send from one inbox
and `inbox_daily_cap` is 0: each project then counts only its own email on
it, and together they can send more than its warm-up allows.

**A bounce rate pauses the inbox for everyone.** When a project's bounce
rate trips its kill switch, that project stops sending as before, and the
inbox the bounced email left from is written to `shared.db`'s
`inbox_pauses`. Every project's Sender holds email from a paused inbox and
logs which project paused it. Resuming sending in that project (`sellbird
--project NAME sending resume`, or Resume on its dashboard) lifts the pause.

Replies, follow-ups to people who replied and transactional mail don't count
against the shared cap, the same as the current cold caps.

Extra inboxes (`sellbird inboxes`) are rows in a project's own database, with
their sign-ins in the system keychain, so each project connects the extra
inboxes it sends from. The main mailbox's token files are the root's; every
project's loop may refresh them, so they are written to a temp file and
swapped in whole.

Every project reads the shared inbox for replies, and a project skips a reply
from someone it has no record of. Gmail and Microsoft 365 reads don't change
the mailbox; an SMTP/IMAP inbox, which marks what it fetches as seen, fetches
without marking in projects mode, so one project never hides a reply from
the project it's for.

## Suppressions and company claims (the shared layer)

`shared.db` is a small SQLite database beside the projects, opened only in
projects mode.

**Suppressions are global.** It has the same `suppressions` table as a project
database. In projects mode `StateManager.add_suppression` writes there (opt-out,
bounce, complaint, manual), and `suppressed_among` checks both the shared table
and the project's own. Listing shows both, removing removes from both, and
undoing an opt-out lifts it from both. The shared `do_not_contact` list is added to every
project's list. An opt-out from project A's email stops project B as well,
which is what the person who opted out expects. A per-project-only suppression
is left out of this version: "not a fit for this product" is already a company
status inside that project.

**A company belongs to one project at a time.** `company_claims(domain,
project, claimed_at, last_touch_at)` records which project is talking to a
company. A company is its domain as Sellbird recorded it (the same key
discovery dedups on); a prospect whose company has no domain is claimed by
its address alone, so a free-mail address never claims everyone on that
provider. A project claims a company as its cold email leaves (next to the
inbox reservation, after every other check), and each later cold email
renews the claim; it's one conditional upsert, so two projects racing for
the same company can't both win. If the email then doesn't go, a claim made
for it is given back; a renewed one stays. While another project's
claim is active (its last touch is within `company_claim_days`), the Sender
moves the email back a day with the note "Another project is in touch with
this company", and the Writer skips drafting for that company, to save
tokens. A claim lapses on its own once the last touch is older than
`company_claim_days`. A reply from the company renews it. `sellbird projects
release DOMAIN` ends a claim early.

## Migrating an existing install

`sellbird projects init NAME` turns a single install into the first project.
It is a dry run unless `--apply` is given. `--apply` refuses while the loop
runs or the dashboard is open (stop both first), writes a
`data/backup-<UTC time>.tar.gz` of everything it moves (a symlinked file is
backed up as its contents), then:

1. moves, into a hidden `projects/.init-NAME/` folder: any extension settings
   (`sellbird.<name>.yaml`; other yaml at the root, a compose file say, is
   not Sellbird's and stays), the trained `skills/product_knowledge.md` and
   `skills/competitive_intel.md`, `data/sellbird.db` with its `-wal`/`-shm`
   files, and last `sellbird.local.yaml` (or `sellbird.yaml` if it is
   trained) as the project's `sellbird.yaml`;
2. creates `data/shared.db`, copies the project's suppressions into it, seeds
   `company_claims` from every company the project has emailed, dated by its
   last send (for mail sent through Instantly, which has no Outbox rows, by
   the prospect's last update), and carries the agent switch if it was off;
3. checks that the moved config loads, then renames the folder to
   `projects/NAME/`, which is when it becomes a project.

If any step fails, every file goes back where it was and a `shared.db` that
`init` created is removed. `.env`, the mailbox tokens, the dashboard and
agent tokens, `prompts/` and the shipped skills stay at the root. When
`sellbird.local.yaml` is the config, the tracked `sellbird.yaml` template
stays at the root too.

`sellbird projects init --reseed` copies every project's suppressions,
claims and agent switch into `shared.db` again, after restoring a backup
say. It adds only what's missing, so running it twice is the same as once.

A second project is a new folder: `sellbird projects new NAME` copies the
untrained `sellbird.yaml` template into it, and `sellbird setup --project
NAME` (or `sellbird train URL --project NAME`) trains it, writing that
project's `sellbird.yaml` and its `skills/`. `sellbird projects check` loads
every project's config and says what's wrong.

## Backward compatibility

With no `projects/` folder:

- the current home is the root, as now, and `shared_dir` is `None`, so no
  shared database is opened and no shared check runs;
- `sellbird run` runs one heartbeat, the lock stays at `data/sellbird.lock`,
  and `/api/projects` returns an empty list, so the dashboard looks the same;
- every command runs as before, and `--project` is refused;
- hosted homes are unchanged: projects are a self-hosted layout, and a hosted
  server already picks its home per request.

The existing test suite runs unchanged. New tests cover:

- two projects' heartbeats running in one process, each seeing only its own
  config, database and drafts;
- ContextVar isolation across `asyncio.gather` and `to_thread`;
- an opt-out in one project blocking the other;
- a company claim holding the second project's first email;
- the shared inbox cap;
- dashboard requests with and without the header, and an unknown name;
- `projects init` on a copy of a single-install layout.

## Not in this version

- Instantly as a project's provider (see The loop).
- Moving a company or a campaign from one project to another.
- Per-project brain accounts or API keys (a project-level `.env` could be added
  later; the root `.env` is shared).
- Separate dashboard logins per project.

## Follow-ups

- The dashboard's agent navigation (`_NavBus`) isn't scoped to a project: an
  agent asking the dashboard to open a page opens it in whichever project
  the pane shows.
- `sellbird mcp` without a project quietly uses the default project, and
  `/api/projects` isn't on the agent's allowlist, so the agent can't list
  the projects to ask which one is meant.
- The log filter that names a line's project (`_ProjectTag`) rewrites
  `record.name`; a formatter field would be cleaner.
