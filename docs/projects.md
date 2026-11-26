# Projects: several objectives in one install

Status: design, for discussion in issue #120. Nothing here changes an install
that has no `projects/` folder.

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
| `writer.email_rules`, prompts and skills overrides | `.env` (API keys, brain login) |
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
  caps;
- `pidfile.py` puts the loop's lock under the process root, not the home.

## Layout

```text
<root>/                       the checkout or SELLBIRD_HOME, as today
  .env                        shared: API keys, brain settings
  sellbird.projects.yaml      optional: settings that span projects
  prompts/  skills/           shared defaults
  data/
    shared.db                 suppressions, company claims, inbox ledger
    dashboard-token           one dashboard login
    agent-token
    m365_token.json           mailbox tokens, shared
    gmail_token.json
  projects/
    product-a/
      sellbird.yaml           this project's ICP, product, offer, email_rules, caps
      data/sellbird.db        this project's companies, prospects, campaigns
      prompts/  skills/       optional: files here win over the shared ones
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
playbooks and a relative `writer.email_rules` path resolve the same way.

Extensions need no change. An extension that keeps its settings beside the
`sellbird.yaml` in use reads the project's own file. Tick steps already get
the cycle's config and state, which are the project's.

## The loop

`sellbird run` in projects mode starts one asyncio task per project. Each task
is created inside `use_home(project_home)`, so the context copy it gets holds
that home, and runs the existing `heartbeat()` unchanged: it loads its own
config, opens its own database and builds its own agents. A project whose
config fails to load is logged and skipped, and the others keep running.
Stopping the process stops every task through the existing stop event.

`sellbird run --project NAME` runs one project. That allows one process per
project (a systemd template unit, say) for anyone who prefers process
isolation; both ways use the same homes and the same shared layer.

Each project has its own lock and PID file in its `data_dir`, so one project
can never run twice. The all-projects runner also holds the root's
`data/sellbird.lock` and writes the root's `data/sellbird.pid`, the files the
dashboard already reads to tell whether the loop is running; `--project NAME`
holds only that project's. The all-projects runner skips, with a line saying
so, a project another process is already running, and one that isn't set up.
Outside projects mode nothing moves (`sellbird/pidfile.py` says all this).

Log lines name their project (`[sellbird.sender:product-a]`), read from the
task's home, so threads it starts are named too.

Each project keeps its own `usage.max_daily_claude_percent`, and they all
spend one Claude account. `sellbird projects check` warns when they add up
to more than 100.

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
- Starting and stopping the loop from the dashboard controls the one process.
  Pausing a project is that project's sending mode, which already lives in its
  config.
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
   provider says the email didn't go.

Replies, follow-ups to people who replied and transactional mail don't count
against the shared cap, the same as the current cold caps.

Extra inboxes (`sellbird inboxes`) are rows in a project's own database, with
their sign-ins in the system keychain, so each project connects the extra
inboxes it sends from. The main mailbox's token files are the root's.

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
provider. A project claims a company when it sends a cold email, and each
later cold email renews the claim; it's one conditional upsert, so two
projects racing for the same company can't both win. While another project's
claim is active (its last touch is within `company_claim_days`), the Sender
moves the email back a day with the note "Another project is in touch with
this company", and the Writer skips drafting for that company, to save
tokens. A claim lapses on its own once the last touch is older than
`company_claim_days`. A reply from the company renews it. `sellbird projects
release DOMAIN` ends a claim early.

## Migrating an existing install

`sellbird projects init NAME` turns a single install into the first project.
It is a dry run unless `--apply` is given, and `--apply` first writes a
`data/backup-<UTC time>.tar.gz` of everything it moves:

1. create `projects/NAME/data/`;
2. move `sellbird.local.yaml` (or `sellbird.yaml` if it is trained) to
   `projects/NAME/sellbird.yaml`, and any extension settings beside it;
3. move `data/sellbird.db` (and its `-wal`/`-shm` files) to
   `projects/NAME/data/sellbird.db`;
4. leave `.env`, the mailbox tokens and the dashboard and agent tokens at the
   root;
5. create `data/shared.db`, copy the project's suppressions into it, and seed
   `company_claims` from every company the project has emailed, dated by its
   last send (for mail sent through Instantly, which has no Outbox rows, by
   the prospect's last update).

When `sellbird.local.yaml` is the config, the tracked `sellbird.yaml`
template stays at the root. The loop must be stopped first; `init` refuses
while the lock is held. A second project is a new folder: `sellbird projects
new NAME` copies the untrained `sellbird.yaml` template into it, and `sellbird
setup --project NAME` (or `sellbird train URL --project NAME`) trains it,
writing that project's `sellbird.yaml` and its `skills/`. `sellbird projects
check` loads every project's config and says what's wrong.

## Backward compatibility

With no `projects/` folder:

- the current home is the root, as now, and `shared_dir` is `None`, so no
  shared database is opened and no shared check runs;
- `sellbird run` runs one heartbeat, the lock stays at `data/sellbird.lock`,
  and `/api/projects` returns an empty list, so the dashboard looks the same;
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

- Moving a company or a campaign from one project to another.
- Per-project brain accounts or API keys (a project-level `.env` could be added
  later; the root `.env` is shared).
- Separate dashboard logins per project.
