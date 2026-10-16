# Install and run MuscleMemory

MuscleMemory needs Python 3.12 or newer.

## Quick start

Install from PyPI. The package is `thewariend-musclememory`; the command is `musclememory`.

```sh
pipx install thewariend-musclememory   # or: uv tool install / pip install thewariend-musclememory
```

Or let your coding agent do it: paste this into Claude Code, Codex or Cursor.

```text
Read https://thewariend.com/musclememory/llms.txt and set up MuscleMemory for this project.
```

Then set it up and connect an agent:

```sh
musclememory init               # folder, database, search model (about 90 MB, once)
musclememory start              # runs in the background on http://127.0.0.1:8080
cd /path/to/your/project
musclememory connect codex      # or claude, cursor, cleanslate
musclememory dashboard          # opens the dashboard, already signed in
```

Restart the agent after `connect`. To add a Ready skill to Codex or Claude Code as a
`SKILL.md`, see [Export skills to your agents](integrations.md#export-skills-to-your-agents). Other commands:

```sh
musclememory status             # running or stopped, and which agents are connected
musclememory stop
musclememory init --port 8090   # use another port
```

Everything private lives in `~/.musclememory`. Set `MM_HOME` to use another folder.

| Path | What it is |
| --- | --- |
| `config.json` | Server address, port, tenant, workspace and database URL |
| `memory.db` | The SQLite database |
| `keys/<agent>.env` | One key per connected agent, owner-only (0600) |
| `hooks/<agent>.json` | Hook settings for that agent, plus its private capture queue |
| `cleanslate.json` | CleanSlate host config, when connected |
| `server.log`, `server.pid` | Background server output and process ID |

## From source

Building the dashboard needs Node 20.19+ or 22.12+.

```sh
git clone https://github.com/TheWariend/MuscleMemory.git && cd MuscleMemory
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
npm ci && npm run build        # builds the dashboard
```

Then run the same `init`, `start` and `connect` steps as above.

## Connecting agents

| Agent | Command | Notes |
| --- | --- | --- |
| Codex | `musclememory connect codex` | Writes `.codex/hooks.json` in the project. |
| Claude Code | `musclememory connect claude` | Writes `.claude/settings.json` in the project. |
| Cursor | `musclememory connect cursor` | Writes `.cursor/hooks.json` in the project. |
| CleanSlate | `musclememory connect cleanslate` | Prints two `export` lines to run before starting CleanSlate. |
| Other MCP clients | — | Point the client at `http://127.0.0.1:8080/mcp` with an API key. |

`connect` creates the agent's key once and reuses it. It keeps a backup of any
existing hook file it changes. Details for each client are in
[integrations.md](integrations.md).

## Workspace scope and project isolation

The workspace is the config key `namespace`, default `"default"`. It groups skills,
checks, approval state and sessions within one tenant on the same server. Projects
using the default setup share that workspace; a repository root is not a recall
boundary. Cross-repository recall is untested, even when the local file check finds
matching paths and text.

`musclememory connect` uses `~/.musclememory/hooks/<agent>.json`, shared by every
project connected with that agent. For a project-specific namespace on the same
server:

1. Create a key for the new workspace in your existing local setup:

   ```sh
   musclememory key --namespace my-project --label my-project-codex --permissions read,write
   ```

   Store the returned `api_key` as `MM_API_KEY=<key>` in a private env file outside
   the repository. Set its permissions to 0600. The key's namespace grant must
   match the hook config.
2. Create a dedicated hook JSON file outside the repository. Use real absolute
   paths for `env_file` and `state_dir`:

   ```json
   {
     "url": "http://127.0.0.1:8080",
     "namespace": "my-project",
     "env_file": "/absolute/private/path/my-project-codex.env",
     "state_dir": "/absolute/private/path/my-project-codex-state"
   }
   ```

   Keep the config private and give the queue directory permissions 0700 if you
   create it yourself. The hooks create it privately when it does not exist.
3. For a project without existing MuscleMemory hooks, install with that config:

   ```sh
   musclememory hooks install --client codex --project /path/to/project \
     --config /absolute/private/path/my-project-codex.json
   ```

   For an already connected project, change the existing MuscleMemory hook
   commands' `--config` path to the dedicated file. The installer preserves
   existing commands, so installing another set would run both workspaces.
   Restart the agent. Use the matching `--client claude` or `--client cursor`
   for those clients, with a dedicated key and config for each.

Changing the shared agent config changes all projects referencing it. Existing
skills remain in their original namespace; choosing another namespace does not
move them. SDK and MCP requests choose the workspace using `visibility_scope`.
The global `init --namespace` option sets the initial local workspace, and is not
a per-project switch for an existing setup.

Native hooks also check current files before delivery: all changed files must
exist, and at least one stored edit must match its before or after text. Partial
matches carry an untrimmed warning; complete mismatches inject nothing. Reads
are bounded to 32 files and 1 MiB per file, without following symlinks. File-check
skip reasons stay in the local queue. Server and MCP recall do not inspect files.
See [the exact rules](how-it-works.md#checking-current-files-in-hooks).

## Dashboard sign-in

- **On this computer:** `musclememory dashboard` makes a one-time link and opens
  it. The link works once and expires after 5 minutes. Use `--no-browser` to
  print it instead.
- **Anywhere else:** paste an API key on the sign-in page. The key stays in that
  browser tab and is cleared when the tab closes.

## API keys

```sh
musclememory key --label my-agent --permissions read,write
```

The key is printed once; store it somewhere private. Without `--tenant` and
`--namespace` the command uses your local setup.

| Permission | Allows |
| --- | --- |
| `read` | See skills and sessions; recall |
| `write` | Record sessions, learn skills, check them, keep or turn off skills |
| `delete` | Delete skills and sessions |
| `admin` | List and revoke keys; reindex |

Revoke a key on the dashboard's **API keys** page. Send keys as
`Authorization: Bearer <key>`. Never put a key in a repository or an agent's
working folder.

## Docker and PostgreSQL

```sh
cp .env.example .env            # set MM_DB_PASSWORD to a strong URL-safe value
docker compose up --build -d
docker compose exec api musclememory key --tenant my-team --namespace default \
  --permissions read,write,delete,admin
```

Compose runs pgvector/PostgreSQL 17, a one-off migration job and the API with the
dashboard. The API listens on `127.0.0.1:${MM_HTTP_PORT:-8080}` only. Containers
run read-only with all capabilities dropped. Embedding model files are kept in a
volume.

For your own PostgreSQL: create the `vector` extension, run `musclememory migrate`
with a schema-owning role, then serve with a role limited to
SELECT/INSERT/UPDATE/DELETE. Each process uses up to 15 database connections.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MM_HOME` | `~/.musclememory` | Local folder used by `init`, `start`, `connect`, `dashboard` |
| `MM_DATABASE_URL` | `sqlite:///~/.musclememory/memory.db` | SQLite or `postgresql+psycopg://…` |
| `MM_API_KEY` | none | Key for `stdio`, `reindex` and SDK clients |
| `MM_EMBEDDING_PROVIDER` | `fastembed` (`remote` if a URL is set) | `fastembed`, `remote` or `lexical-hash` |
| `MM_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model |
| `MM_EMBEDDING_DIM` | `384` | Vector size, 1–2000 |
| `MM_EMBEDDING_URL` / `MM_EMBEDDING_KEY` | none | Optional HTTPS embedding endpoint and its key |
| `MM_EMBEDDING_CACHE_DIR` | provider default | Where local model files are kept |
| `MM_SEMANTIC_MIN_SIMILARITY` | `0.40` | Cosine floor for meaning matches |
| `MM_GUIDANCE_TOKEN_BUDGET` | `350` | Pointer size limit, 128–2000 |
| `MM_LINK_FANOUT_LIMIT` | `8` | Files shared by more skills create no links |
| `MM_MIN_SUCCESSES` | `2` | Passing checks before a skill is ready (minimum 2) |
| `MM_MIN_RELIABILITY` | `0.65` | Smoothed reliability needed to be ready |
| `MM_STALE_DAYS` | `90` | Days before checks count as stale |
| `MM_REQUIRE_PROVEN_TEST` | `false` | When true, hand out a skill whose test already existed only after a session saw that test fail on the original code and pass after the fix |
| `MM_AUTO_APPROVE` | `false` | When true, skills don't wait for a person's OK; they still need their two independent checks |
| `MM_TRACE_RETENTION_DAYS` | `30` | Days recorded sessions are kept |
| `MM_REQUESTS_PER_MINUTE` | `120` | Rate limit per key |
| `MM_BOUNDARY_LLM_URL` / `_KEY` / `_MODEL` | none / none / `boundary-refinement` | Optional HTTPS note refinement (value-free) |
| `MM_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,testserver` | Accepted Host headers |
| `MM_ALLOWED_ORIGINS` | `http://127.0.0.1:*,http://localhost:*` | Accepted browser origins |
| `MM_DEPLOYMENT_MODE` | `local` | `local` or `hosted` |
| `MM_PUBLIC_URL` | none | Root HTTPS URL, required in hosted mode |

Remote endpoints must use HTTPS without credentials in the URL. `MM_API_URL` can
override the hook endpoint. Hook workspace selection uses the JSON `namespace`
field, defaulting to `"default"`; `MM_NAMESPACE` does not override that field.

## Upgrading

```sh
musclememory stop
cp ~/.musclememory/memory.db ~/memory-backup.db    # or sqlite3 memory.db ".backup …"
pipx upgrade thewariend-musclememory                # or, from a clone: pip install -e .
musclememory migrate                                # with MM_DATABASE_URL if not local
musclememory start
```

Back up before every migration and test restores. For PostgreSQL,
`scripts/database_backup.py` takes and restores backups.

After changing the embedding model or dimension, rebuild the vectors. History and
checks are kept:

```sh
MM_API_KEY=<admin key> musclememory reindex --namespace default
```

Run `musclememory purge` from a scheduler to delete expired sessions for good.

## Running it remotely

- Put MuscleMemory behind a TLS proxy. Keep the backend bound to a private address.
- Set exact `MM_ALLOWED_HOSTS` and `MM_ALLOWED_ORIGINS`. Do not log request bodies.
- `MM_DEPLOYMENT_MODE=hosted` enforces this. It requires PostgreSQL, a root HTTPS
  `MM_PUBLIC_URL`, host allowlists without wildcards or `testserver` that include
  the public host, and HTTPS origin allowlists without wildcards that include the
  public URL. Add `127.0.0.1:8080` to the hosts for private health checks.
- Use `sslmode=verify-full` for a remote database.
- There is no built-in OAuth, SSO or automatic certificate management yet.
