# pg-multi-mcp

**English** · [Português](README.pt-BR.md)

A local [MCP](https://modelcontextprotocol.io) server that gives an AI assistant access to **many named database
connections at once**. You say *"list the tables in the staging database"* and the assistant finds the right
connection by name or alias, reuses an already-open pool, and closes pools that sit idle.

Today it supports **PostgreSQL**. The core (connection registry, name resolution, pool lifecycle, config reload)
does not depend on the engine: every connection has an `engine` field and PostgreSQL-specific code lives in its own
subpackage, so other engines can be added later.

---

## Table of contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Quick start](#quick-start)
5. [Configuring connections](#configuring-connections)
6. [Passwords and secrets](#passwords-and-secrets)
7. [Registering the server in an MCP client](#registering-the-server-in-an-mcp-client)
8. [Command-line options](#command-line-options)
9. [Environment variables](#environment-variables)
10. [Tools reference](#tools-reference)
11. [How a connection is chosen](#how-a-connection-is-chosen)
12. [Connection lifecycle](#connection-lifecycle)
13. [Editing the config while the server runs](#editing-the-config-while-the-server-runs)
14. [Access modes and security](#access-modes-and-security)
15. [Example prompts](#example-prompts)
16. [Troubleshooting](#troubleshooting)
17. [Project layout and adding an engine](#project-layout-and-adding-an-engine)
18. [Development](#development)
19. [License](#license)

---

## Features

| Feature | Description |
|---|---|
| Many named connections | Declared in a JSON file, in the client's `.mcp.json`, or added from the chat |
| Pick a database per call | Every database tool takes a `connection` argument; `use_connection` sets a session default |
| Forgiving lookup | Matches name, aliases, tags and description, ignoring case and accents, with "did you mean" suggestions |
| Access mode per connection | `restricted` (read-only, the default) or `unrestricted`, plus a global read-only cap |
| Lazy, reused pools | Opened on first use, reused afterwards, closed when idle, evicted LRU when over the limit |
| Fast, real connection errors | Wrong password, DNS or closed port are reported in seconds, not as a generic timeout |
| Live config reload | Edits to the config file apply on the next call; unchanged connections keep their pools |
| PostgreSQL analysis tools | Explain plans (including hypothetical indexes via HypoPG), health checks, index recommendations, top queries |
| Safe output size | `execute_sql` truncates large results (`max_rows`, default 500) |
| No secret leakage | Passwords never appear in listings or error messages |

## Requirements

- Python **3.12+**
- Network access to the databases you configure
- Optional: [`uv`](https://docs.astral.sh/uv/) if you want to run it with `uvx` instead of a local virtualenv
- Optional PostgreSQL extensions for some tools: `pg_stat_statements` (top queries, workload analysis) and
  `hypopg` (hypothetical indexes)

## Installation

### Option A — local virtualenv

```powershell
git clone https://github.com/Snarloff/pg-multi-mcp.git
cd pg-multi-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

The command is now at `.venv\Scripts\pg-multi-mcp.exe` (Windows) or `.venv/bin/pg-multi-mcp` (Linux/macOS).

### Option B — `uvx` straight from git (no manual build)

`uvx` downloads, builds and caches it from the repository on first run:

```powershell
uvx --from git+https://github.com/Snarloff/pg-multi-mcp pg-multi-mcp --help
```

This tracks the default branch. To pin a version, append a tag or commit, e.g.
`git+https://github.com/Snarloff/pg-multi-mcp@v0.1.0`, so everyone runs the same code; change it to upgrade.

## Quick start

1. **Install** (see above).
2. **Create the config file** at `~/.pg-multi-mcp/connections.json`
   (Windows: `C:\Users\<you>\.pg-multi-mcp\connections.json`):
   ```json
   {
     "connections": {
       "local": {
         "url": "postgresql://postgres@localhost:5432/postgres",
         "access_mode": "unrestricted",
         "description": "Local development database",
         "aliases": ["dev"]
       }
     }
   }
   ```
3. **Provide the password** through pgpass or an environment variable (see [Passwords](#passwords-and-secrets)).
4. **Register the server** in your MCP client:
   ```powershell
   claude mcp add pg-multi-mcp --scope user -- C:\path\to\pg-multi-mcp\.venv\Scripts\pg-multi-mcp.exe
   ```
5. **Restart the client** and ask: *"list my database connections"*, then *"list the schemas in dev"*.

## Configuring connections

Connections can come from two sources, loaded in this order:

| Source | Use it for | Persisted | Editable by tools |
|---|---|---|---|
| 1. Config file `connections.json` | Your main registry of databases | Yes | Yes (`add_connection`, `remove_connection`) |
| 2. Client config `.mcp.json` (`env` block) | Quick or project-specific connections | Session only | Only removed from the session |

If a name exists in both, **the `.mcp.json` one wins for the session**; the file entry stays untouched on disk and
comes back if the override is removed. If the `DATABASE_URI` variable is set, it becomes a session connection named
`default` (read-only).

### 1. Config file

- Default location: `~/.pg-multi-mcp/connections.json`.
- Override with `--config <path>` or the `PG_MULTI_MCP_CONFIG` variable. `~` is expanded.
- A missing file is fine: the server starts with no connections.
- Template: [connections.example.json](connections.example.json).

```json
{
  "default": "local",
  "settings": {
    "idle_timeout_seconds": 600,
    "max_open_connections": 10
  },
  "connections": {
    "local": {
      "engine": "postgres",
      "url": "postgresql://postgres:${PG_LOCAL_PASSWORD}@localhost:5432/postgres",
      "access_mode": "unrestricted",
      "description": "Local development database",
      "aliases": ["dev", "localhost"],
      "tags": ["development"]
    },
    "sales_staging": {
      "url": "host=staging.example.com port=5432 dbname=sales user=readonly sslmode=require",
      "description": "Sales - staging (read-only)",
      "aliases": ["sales staging"],
      "tags": ["staging", "sales"],
      "max_pool_size": 2,
      "query_timeout": 30
    }
  }
}
```

**Top-level keys**

| Key | Default | Description |
|---|---|---|
| `default` | `null` | Connection used when a tool call does not name one (see [resolution](#how-a-connection-is-chosen)) |
| `settings.idle_timeout_seconds` | `600` | Close a pool after this many idle seconds (`0` = never) |
| `settings.max_open_connections` | `10` | Maximum pools open at the same time; the least recently used idle pool is closed beyond that |
| `connections` | `{}` | Map of connection name → definition |

**Connection fields**

| Field | Required | Default | Description |
|---|---|---|---|
| *(key)* | yes | — | Connection name: letters, digits, `_`, `-`, `.`; up to 63 chars |
| `url` | yes | — | libpq URI (`postgresql://user@host:port/db?sslmode=require`) or key/value DSN (`host=... dbname=...`). `${VAR}` placeholders are expanded from environment variables at connect time |
| `engine` | no | `postgres` | Database engine (`postgresql` and `pg` are accepted as aliases) |
| `access_mode` | no | `restricted` | `restricted` = read-only; `unrestricted` = any SQL, including DML/DDL |
| `description` | no | `""` | Free text; used by search |
| `aliases` | no | `[]` | Alternative names the assistant can use to find the connection |
| `tags` | no | `[]` | Grouping/search labels such as `production`, `staging` |
| `max_pool_size` | no | `3` | Maximum connections in this pool |
| `query_timeout` | no | `30` | Query timeout in seconds for `restricted` connections |

Names and aliases must be unique across all connections after normalization (case, accents and separators are
ignored), so `Sales-Staging`, `sales_staging` and `Sales Staging` are the same key.

### 2. `.mcp.json` (client config)

Every environment variable named `PG_MULTI_MCP_CONN_<NAME>` becomes a connection called `<name>` (lower-cased).
Its value is either a URL/DSN or a JSON object with the same fields as the file. `PG_MULTI_MCP_DEFAULT` sets the
default connection for the session. Template: [mcp.example.json](mcp.example.json).

```json
{
  "mcpServers": {
    "pg-multi-mcp": {
      "command": "C:\\path\\to\\pg-multi-mcp\\.venv\\Scripts\\pg-multi-mcp.exe",
      "args": ["--access-mode", "restricted"],
      "env": {
        "PG_PROJECT_PASSWORD": "${PG_PROJECT_PASSWORD}",
        "PG_MULTI_MCP_CONN_PROJECT_DEV": "postgresql://app:${PG_PROJECT_PASSWORD}@localhost:5432/project_dev",
        "PG_MULTI_MCP_CONN_PROJECT_STG": "{\"url\": \"postgresql://readonly@staging.example.com/project\", \"aliases\": [\"staging\"]}",
        "PG_MULTI_MCP_DEFAULT": "project_dev"
      }
    }
  }
}
```

> `.mcp.json` is usually committed to the project repository. **Never put a literal password in it**: use
> `${VAR}` (expanded by the client from your environment) or pgpass.

## Passwords and secrets

Pick one of these, from most to least recommended:

**1. pgpass file (no password in any config).** libpq reads it automatically when the URL has no password.

| OS | Location |
|---|---|
| Windows | `%APPDATA%\postgresql\pgpass.conf` |
| Linux/macOS | `~/.pgpass` (must be `chmod 600`) |

Format: `host:port:database:user:password`, `*` is a wildcard. One line can cover many databases:

```
*:5432:*:readonly_user:<password>
```

**2. `${VAR}` placeholder.** Put `${PG_LOCAL_PASSWORD}` in the URL and define the variable:

```powershell
setx PG_LOCAL_PASSWORD "<password>"   # then restart the MCP client so it sees the variable
```

The placeholder is stored as-is and only expanded when connecting. If the variable is missing, the call fails with
a clear message naming it.

**3. Literal password in `connections.json`.** Works, but the file is plain text readable by anything running as
your user. `add_connection` warns when it is about to save a literal password.

**URL encoding:** in the URI form, passwords containing `@ : / # ? %` must be percent-encoded. The key/value DSN
form (`password=...`) does not need encoding as long as the password has no spaces or quotes.

## Registering the server in an MCP client

**Claude Code (user scope, available in every project):**

```powershell
claude mcp add pg-multi-mcp --scope user -- C:\path\to\pg-multi-mcp\.venv\Scripts\pg-multi-mcp.exe --access-mode restricted
```

**Claude Code (project scope):** create `.mcp.json` in the project root as shown in
[`.mcp.json` (client config)](#2-mcpjson-client-config).

**Claude Desktop:** edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) and add the same `mcpServers` entry.

**With `uvx`:**

```json
"pg-multi-mcp": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/Snarloff/pg-multi-mcp", "pg-multi-mcp", "--access-mode", "restricted"]
}
```

After changing the client config, restart or reconnect the server (in Claude Code: `/mcp`).

## Command-line options

| Option | Default | Description |
|---|---|---|
| `--config PATH` | `$PG_MULTI_MCP_CONFIG` or `~/.pg-multi-mcp/connections.json` | Connections file |
| `--access-mode restricted` | *(per connection)* | Global cap: forces **every** connection to read-only, regardless of the file |
| `--access-mode unrestricted` | | Same as omitting it: each connection uses its own `access_mode` |
| `--idle-timeout N` | file setting, else `600` | Seconds before an idle pool is closed (`0` = never). Overrides the file |
| `--max-open N` | file setting, else `10` | Maximum open pools (LRU eviction). Overrides the file |
| `--lock-config` | off | Disables `add_connection` and `remove_connection` |
| `--no-auto-reload` | off | Do not reload the config file when it changes |
| `--transport stdio\|streamable-http` | `stdio` | MCP transport |
| `--host HOST` | `localhost` | Bind address for `streamable-http` |
| `--port PORT` | `8000` | Port for `streamable-http` |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (logs go to stderr) |

Recommended for production databases: `--access-mode restricted --lock-config`.

## Environment variables

| Variable | Description |
|---|---|
| `PG_MULTI_MCP_CONFIG` | Path to the connections file (same as `--config`) |
| `PG_MULTI_MCP_CONN_<NAME>` | Declares a session connection `<name>`; value is a URL/DSN or a JSON object |
| `PG_MULTI_MCP_DEFAULT` | Default connection for the session (takes precedence over the file's `default`) |
| `DATABASE_URI` | Compatibility: becomes a read-only session connection named `default` |
| `PG_MULTI_MCP_INCLUDE_DTA_TRACE` | `true` to include the index advisor's internal trace in its output (default `false`) |
| Any `${VAR}` you reference | Expanded inside connection URLs at connect time |

## Tools reference

All database tools accept an optional `connection` (name or alias). Every database response starts with a header
line such as `[conexão: local | modo: unrestricted]`, so you can always see where a command ran. Server messages
are in Portuguese.

### Connection management

| Tool | Arguments | What it does |
|---|---|---|
| `list_connections` | `search?` | Lists connections: engine, target (host/port/db/user, never the password), access mode, source (`file`/`env`/`runtime`), pool status, idle time, active/default flags. Shows `config_error` if the last file edit was invalid |
| `use_connection` | `connection` | Validates the connection (opens it) and makes it the session's active connection |
| `open_connection` | `connection?` | Opens or reuses the pool and returns database, user, server version and latency |
| `close_connection` | `connection?`, `close_all=false` | Closes one pool or all of them. The definition is kept and reopens on next use |
| `add_connection` | `name`, `url`, `access_mode="restricted"`, `description`, `aliases`, `tags`, `persist=true`, `test=true`, `overwrite=false`, `engine="postgres"` | Adds a connection. With `test=true` it connects first and adds nothing on failure. With `persist=true` it is written to the file; otherwise it lives only in the session |
| `remove_connection` | `connection` | Closes and removes a connection (also from the file if it came from there). Removing an env override brings back the file entry |
| `reload_connections` | — | Re-reads the file now and reports `added`, `changed`, `removed`, `unchanged` |

### Database (PostgreSQL)

| Tool | Main arguments | What it does |
|---|---|---|
| `list_schemas` | — | Lists schemas and classifies them as user/system |
| `list_objects` | `schema_name`, `object_type="table"` | Lists tables, views, sequences or extensions |
| `get_object_details` | `schema_name`, `object_name`, `object_type="table"` | Columns, constraints and indexes of a table/view; details of a sequence or extension |
| `execute_sql` | `sql`, `max_rows=500` | Runs SQL. Read-only on `restricted` connections. Results above `max_rows` are truncated and flagged |
| `explain_query` | `sql`, `analyze=false`, `hypothetical_indexes=[]` | Execution plan; `analyze=true` runs the query for real timings; hypothetical indexes need HypoPG |
| `analyze_workload_indexes` | `max_index_size_mb=10000` | Recommends indexes from the workload in `pg_stat_statements` |
| `analyze_query_indexes` | `queries` (up to 10), `max_index_size_mb=10000` | Recommends indexes for the given queries |
| `analyze_db_health` | `health_type="all"` | Health checks: `index`, `connection`, `vacuum`, `sequence`, `replication`, `buffer`, `constraint`, `all` (comma-separated list allowed) |
| `get_top_queries` | `sort_by="resources"`, `limit=10` | Slowest or most resource-intensive queries (`resources`, `mean_time`, `total_time`); needs `pg_stat_statements` |

## How a connection is chosen

**When `connection` is given**, it is matched in this order:

1. Exact name.
2. Normalized name or alias: lower case, no accents, no spaces/`_`/`-`/`.`. So `"Relatórios"`, `"relatorios"` and
   `"RELATORIOS"` are the same.
3. Not found: the error lists close matches (`"Did you mean: ..."`) and partial matches.

**When `connection` is omitted:**

1. The active connection (set by `use_connection`).
2. The default (`PG_MULTI_MCP_DEFAULT`, otherwise the file's `default`).
3. The only connection, if exactly one exists.
4. Otherwise an error asks the assistant to specify one and lists the available names. The server never guesses
   between several databases.

`list_connections(search="...")` searches names, aliases, tags and descriptions with the same normalization.
The server also gives the assistant instructions to look up connections this way and to always pass `connection`
explicitly when there is more than one.

## Connection lifecycle

1. **Lazy:** no pool is opened at startup.
2. **First use:** a single test connection is made (fails fast with the real error), then the pool is opened.
3. **Reuse:** later calls reuse the pool.
4. **Idle close:** every 30 seconds, pools idle for longer than `idle_timeout_seconds` are closed. Pools in use are
   never closed.
5. **LRU eviction:** opening a pool beyond `max_open_connections` closes the least recently used idle pool.
6. **Self-healing:** a query error invalidates the pool; it is recreated on the next call.
7. **Shutdown:** all pools are closed when the server exits.

Sessions are visible in `pg_stat_activity` with `application_name = 'pg-multi-mcp'`. A closed pool reopens
transparently, so `close_connection` is about freeing server connections, not about access.

## Editing the config while the server runs

There is no need to restart after editing `connections.json`:

- **Automatic:** before each use the server compares the file's modification time and reloads it if it changed.
- **Manual:** `reload_connections` reloads now and reports what changed.
- **Pools:** unchanged connections keep their open pools; changed and removed ones are closed.
- **Invalid edits:** if the file has invalid JSON (or a duplicate name/alias), the previous version stays in effect
  and `list_connections` shows `config_error` with line and column only (never file content) until you fix it.
- **No lost edits:** `add_connection`/`remove_connection` reload before writing, so they never overwrite manual
  changes, and they are blocked while the file is invalid.
- **`.mcp.json` connections** are not affected by reloads.

Changes to `.mcp.json` itself are only read when the client restarts the server.

## Access modes and security

**`restricted` (default)**

- SQL is parsed (`pglast`) and checked against an allowlist of statements (essentially `SELECT`, `EXPLAIN`,
  `SHOW` and cursors) and of functions.
- Everything runs inside a `READ ONLY` transaction, so PostgreSQL itself rejects any write that gets through.
- Queries are aborted after `query_timeout` seconds.

**`unrestricted`**: any SQL, including `INSERT`, `UPDATE`, `DELETE`, `DROP`. Use only for databases you own.

**Global cap:** `--access-mode restricted` forces every connection to read-only, even if someone sets
`unrestricted` in the file or adds one from the chat. `--lock-config` additionally prevents adding or removing
connections.

**Recommendations**

- The real guarantee is the database user's privileges: connect with a **read-only database user**.
- Query results are sent to the AI model. For databases with personal data, use read-only access, prefer
  aggregated or anonymized queries, and make sure you have a legal basis to process that data. This tool does not
  solve data-protection compliance (LGPD, GDPR...).
- Prefer `sslmode=require` (or stricter) for remote databases.
- Passwords are never included in `list_connections` output or error messages.
- The server sends data only to the MCP client that launched it; it calls no other external service.

## Example prompts

| You say | What the assistant does |
|---|---|
| "Which databases do I have?" | `list_connections()` |
| "List the tables in sales staging" | `list_connections(search="sales")` → `list_objects(schema_name="public", connection="sales_staging")` |
| "Use dev from now on" | `use_connection("dev")`, later calls omit `connection` |
| "Compare the `users` table between dev and staging" | Two `get_object_details` calls, one per connection |
| "Why is this query slow in staging?" | `explain_query(sql=..., connection="sales_staging")` |
| "Is the staging database healthy?" | `analyze_db_health(connection="sales_staging")` |
| "I added a database to the file" | Nothing needed (auto-reload), or `reload_connections()` |
| "Close all connections" | `close_connection(close_all=true)` |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `fe_sendauth: no password supplied` | No password in the URL and none in pgpass. Add a pgpass line or a `${VAR}` placeholder |
| `password authentication failed` | Wrong user/password, or the password needs URL encoding in URI form |
| `Variável(is) de ambiente não definida(s): X` | The URL references `${X}` but the variable is not visible to the server. After `setx`, restart the MCP client |
| `connection timeout expired` / `connection refused` | Host or port unreachable: VPN, firewall or wrong port |
| `Há várias conexões e nenhuma está ativa` | Several connections and no default/active one: pass `connection`, call `use_connection` or set `default` |
| `Conexão 'x' não encontrada. Você quis dizer: ...` | Typo or missing alias; add an alias to the file |
| `config_error` in `list_connections` | The file has invalid JSON or a duplicate name/alias; the message shows line/column |
| `conflita com a conexão existente` / `usado por` | Two connections share a name or alias after normalization |
| Restricted query rejected | The statement or a function is not on the read-only allowlist. Use a `SELECT` or an `unrestricted` connection |
| Edits to `.mcp.json` not applied | The client reads it only on (re)start: restart or reconnect the server |
| `WinError 32` when reinstalling (`pip install -e .`) | The MCP client is running `pg-multi-mcp.exe`, so Windows locks it. Stop or disable the server in the client first (Claude Code: `/mcp`), reinstall, then reconnect. Code changes in `src/` do not need a reinstall, only a server restart |
| Need more detail | Run with `--log-level DEBUG`; logs go to stderr / the client's MCP log |

## Project layout and adding an engine

```
src/
└── pg_multi_mcp/            Python package (entry point: pg_multi_mcp:main)
    ├── __init__.py          main(): starts the event loop (SelectorEventLoop on Windows)
    ├── server.py            MCP tools and command-line options
    ├── connections.py       Registry, name resolution, pools, LRU, idle reaper, reload, persistence
    └── postgres/            PostgreSQL-specific code
        ├── sql/             Driver, pool, read-only SQL validation, extensions
        ├── explain/         Explain plans and hypothetical indexes
        ├── index/           Index advisor
        ├── database_health/ Health checks
        └── top_queries/     pg_stat_statements reports
tests/
├── unit/test_connections.py Registry, reload, env, persistence
├── unit/test_server.py      Tool routing and responses
└── unit/postgres/           PostgreSQL module tests
```

**Adding another engine (roadmap):**

1. Add a value to `Engine` in [connections.py](src/pg_multi_mcp/connections.py).
2. Create `src/pg_multi_mcp/<engine>/` with a driver: pool, query execution and a read-only mode.
3. Make `ConnectionManager` pick the pool/driver from the connection's `engine`.
4. Dispatch the generic tools (`list_schemas`, `execute_sql`, ...) by engine; PostgreSQL-only tools reject other
   engines.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e . pytest pytest-asyncio ruff==0.14.13
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\ruff.exe check src tests
```

Integration tests that create tables need `PG_MULTI_MCP_TEST_DATABASE_URL` pointing to a **disposable** database;
without it they are skipped.

## License

MIT — see [LICENSE](LICENSE).
