# CCCC DB

A replacement for the Google Sheets archive of the **Cryptic Clue Chat Chains**
(CCCC), an informal competition in [The Sphinx's Lair](https://chat.stackexchange.com/rooms/14524)
on Puzzling Stack Exchange.

The archive of roughly 10,000 clues is served as a searchable web UI and a JSON
API, and new clues are picked up automatically from the chat room by a small
daemon that watches the room and writes accepted clues straight to the database.

## Features

- **PostgreSQL-backed** with full-text search across clue text, solution,
  explanation, author, and solver, using a custom `cccc_english` text-search
  configuration (English stemmer, stopwords removed so common words are
  searchable).
- **Stack Exchange OAuth2** authentication — log in with your SE account.
- **Editor / admin permissions** — diamond moderators are promoted to admin
  automatically; admins grant editor access to others. Clue and sequence writes
  require editor privileges.
- **REST API** with automatic OpenAPI/Swagger docs at `/api/docs`.
- **HTMX web UI** — live search, pagination, clue detail, user profiles, stats,
  sequences — no SPA build step.
- **Sequences** (author runs, themes, and loose tags) grouped many-to-many over
  clues, with an editor UI and an "auto-add the author's last N clues" helper.
- **Chat ingest daemon** — connects to the CCCC room and ingests clues as they
  are posted, deduplicating on the SE chat message ID, and announces an author's
  1st/50th clue milestone back into the room.
- **Solution-ingest pipeline (Stage 2)** — after each clue the daemon collects
  the chat between it and the next clue, extracts candidate answers with a
  deterministic, extraction-first pipeline, and queues them for editor review.
  An optional OpenAI-compatible LLM is used only to *reconstruct* answers from
  wordplay, never to solve a clue cold.
- **Embeddable `.png` previews** — `/clue/{n}.png`, `/user/{name}.png`, and
  `/sequences/{id}.png` are canonical URLs that serve a rendered PNG to image
  fetchers (SE chat) and the real HTML page to browsers.
- **Transcript link parser** — paste a `chat.stackexchange.com/transcript/...`
  URL and auto-fill the clue entry form. Uses the RSS search feed
  (`/feeds/search/CCCC?room=14524`) for the 30 most recent CCCC messages, with
  an events API fallback for older messages.
- **CSV import and export** — bulk import the legacy Google Sheet export, and
  download the whole archive as CSV from `/api/clues/export.csv`.

## Tech Stack

| Layer    | Technology                     | Why                                                   |
|----------|--------------------------------|-------------------------------------------------------|
| Database | PostgreSQL 16                  | Full-text search (tsvector + GIN), generated columns  |
| Backend  | Python 3.12 + FastAPI          | Automatic OpenAPI docs, async, Pydantic validation    |
| Frontend | HTMX + Jinja2                  | Interactive UI without SPA complexity                 |
| Auth     | Stack Exchange OAuth2          | SE-native, diamond-moderator detection                |
| Chat     | vendored `sechat` + `requests` | Reads the Cloudflare-protected SE chat room           |
| Images   | Pillow (PIL) + DejaVu fonts    | Server-rendered `.png` previews                       |

## Architecture: two processes

The same database and code are shared by **two independent processes**:

1. **Web app** (`app.main:app`, uvicorn) — FastAPI API + Jinja/HTMX pages.
2. **Ingest daemon** (`python -m app.services.ingest`) — connects to SE chat via
   `sechat`, watches for new clues, writes them to the DB, and runs the solution
   detection pipeline. It is independent of FastAPI's request/response/auth
   layer, which is why clue writes go through a shared service module
   (`app/services/clue_service.py`) rather than through the API routes.

On a clue's first or 50th-by-author milestone the daemon also posts a
congratulatory message back into the chat room.

## Quick Start (Docker)

```bash
# 1. Clone
git clone https://github.com/themoonisacheese/pse_cccc_db.git
cd pse_cccc_db

# 2. Configure
cp .env.example .env
# Edit .env — set SECRET_KEY and (optionally) SE OAuth / bot credentials

# 3. Provide the CSV export
#    The app container imports ./data.csv (gitignored) on first start and skips
#    the import if the clues table is already populated. Without the file the
#    container's startup command fails. Download it with:
python3 -c "import urllib.request,shutil; \
u='https://copyparty.poggers.website/sharex/Puzzling%20Stack%20Exchange%20Chat%20Cryptic%20Clue%20Chains%20-%20Sheet1.csv?k=n9t9hBBY'; \
urllib.request.urlretrieve(u,'data.csv')"

# 4. Run
docker compose up --build
```

This starts PostgreSQL and the app on <http://localhost:8000>. The app service
runs `scripts/import_csv.py --csv /app/data.csv --if-empty` before uvicorn, so
the full archive is imported on the first run and skipped thereafter.

To also run the chat ingest daemon:

```bash
docker compose up -d ingest
```

The ingest service first runs `scripts/backfill_message_ids.py`, then starts
the daemon. It requires `SE_BOT_EMAIL` / `SE_BOT_PASSWORD` in `.env`.

To (re-)import, or import without Docker:

```bash
docker compose exec app python scripts/import_csv.py            # downloads if no --csv
docker compose exec app python scripts/import_sequences.py      # themes + author runs
```

## Local Development (without Docker)

```bash
# 1. Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Start PostgreSQL and create the database
createdb cccc

# 3. Configure
cp .env.example .env
# Edit DATABASE_URL / DATABASE_URL_SYNC to point at your local PostgreSQL
# (the defaults already target localhost:5432/cccc).

# 4. Import data
python scripts/import_csv.py --limit 100   # test with 100 clues
python scripts/import_csv.py               # full import (downloads the CSV)
python scripts/import_sequences.py         # themes and author sequences

# 5. Run the web app
uvicorn app.main:app --reload

# 6. (Optional) run the ingest daemon in a second terminal
python -m app.services.ingest
```

## Configuration

All settings are read from the environment (and from `.env` if present) by
`app/core/config.py`. See `.env.example` for a documented template.

| Variable                  | Default                                                   | Purpose |
|---------------------------|-----------------------------------------------------------|---------|
| `DATABASE_URL`            | `postgresql+asyncpg://cccc:cccc@localhost:5432/cccc`      | Async URL used by the app |
| `DATABASE_URL_SYNC`       | `postgresql+psycopg2://cccc:cccc@localhost:5432/cccc`     | Sync URL used by scripts and migrations |
| `POSTGRES_USER/PASSWORD/DB` | `cccc`                                                  | Used by the Docker Compose `db` service |
| `DEBUG`                   | `true`                                                    | When false, the session cookie is marked `Secure` |
| `SECRET_KEY`              | `dev-insecure-secret-change-me`                           | Signs the session and OAuth-state cookies — change this |
| `SE_CLIENT_ID` / `SE_CLIENT_SECRET` | empty                                          | Stack Apps OAuth2 credentials |
| `SE_KEY`                  | empty                                                     | Stack Apps API key for SE API calls |
| `SE_OAUTH_REDIRECT`       | `http://localhost:8000/api/auth/callback`                 | Must match the registered redirect URI |
| `SE_SITE`                 | `puzzling.stackexchange.com`                              | Site used for user lookups / moderator checks |
| `SE_CHAT_ROOM_ID`         | `14524`                                                   | The CCCC room |
| `SE_BOT_EMAIL` / `SE_BOT_PASSWORD` | empty                                          | Bot account for chat access; leave empty to disable chat features |
| `ROOM_OWNER_IDS`          | empty                                                     | Reserved allowlist; **not currently enforced** (see Auth) |
| `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | `50` / `500`                                  | Pagination bounds |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | empty                                 | Optional OpenAI-compatible endpoint used by the Stage 2 worker |
| `LLM_MAX_RETRIES` / `LLM_BACKOFF_BASE` | `5` / `2.0`                                    | Retry policy for the LLM worker |
| `SOLUTION_AUTO_ACCEPT_THRESHOLD` | `0.0`                                             | Reserved; currently nothing is auto-accepted |
| `SOLUTION_NOISE_USER_IDS` | empty                                                     | Reserved; window noise filtering is not yet wired to config |

## REST API

The API is under `/api` with interactive docs at `/api/docs` (Swagger UI) and
the schema at `/api/openapi.json`.

| Method   | Endpoint                              | Description                                  | Auth        |
|----------|---------------------------------------|----------------------------------------------|-------------|
| `GET`    | `/api/clues`                          | Search/filter/paginate clues                 | Public      |
| `GET`    | `/api/clues/export.csv`               | Export the full archive as CSV               | Public      |
| `GET`    | `/api/clues/{id}`                     | Get a single clue by DB ID                   | Public      |
| `POST`   | `/api/clues`                          | Create a clue (mid-chain insert supported)   | Editor      |
| `PUT`    | `/api/clues/{id}`                     | Update a clue                                | Editor      |
| `DELETE` | `/api/clues/{id}`                     | Delete a clue                                | Editor      |
| `GET`    | `/api/clues/stats/overview`           | Aggregate stats                              | Public      |
| `GET`    | `/api/clues/stats/authors`            | Author leaderboard                           | Public      |
| `GET`    | `/api/clues/stats/solvers`            | Solver leaderboard                           | Public      |
| `GET`    | `/api/sequences`                      | List sequences (filter by `seq_type`, `q`)   | Public      |
| `GET`    | `/api/sequences/{id}`                 | Get a sequence with its clues                | Public      |
| `POST`   | `/api/sequences`                      | Create a sequence                            | Editor      |
| `PUT`    | `/api/sequences/{id}`                 | Update a sequence                            | Editor      |
| `DELETE` | `/api/sequences/{id}`                 | Delete a sequence                            | Admin       |
| `PUT`    | `/api/sequences/{id}/clues`           | Replace full clue membership                 | Editor      |
| `POST`   | `/api/sequences/{id}/clues/auto-by-author` | Add the author's last N clues           | Editor      |
| `POST`   | `/api/sequences/{id}/clues/{clue_id}` | Add one clue to a sequence                   | Editor      |
| `DELETE` | `/api/sequences/{id}/clues/{clue_id}` | Remove one clue from a sequence              | Editor      |
| `GET`    | `/api/review/clues/{id}/candidates`   | List pending solution candidates             | Editor      |
| `POST`   | `/api/review/candidates/{id}/approve` | Approve a candidate as the solution          | Editor      |
| `POST`   | `/api/review/candidates/{id}/reject`  | Reject a candidate                           | Editor      |
| `POST`   | `/api/review/clues/{id}/manual-solve` | Write a solution directly (form-encoded)     | Editor      |
| `GET`    | `/api/transcript/parse?url=`          | Parse a transcript link                      | Public      |
| `GET`    | `/api/auth/login`                     | Initiate SE OAuth2 flow                      | Public      |
| `GET`    | `/api/auth/callback`                  | OAuth2 callback                              | Public      |
| `GET`    | `/api/auth/logout`                    | Clear the session                            | Session     |
| `GET`    | `/api/auth/me`                        | Current user info (or `null`)                | Session     |
| `GET`    | `/api/admin/editors`                  | List editors                                 | Admin       |
| `GET`    | `/api/admin/users`                    | List all known users                         | Admin       |
| `POST`   | `/api/admin/editors/{se_user_id}`     | Grant editor privileges                      | Admin       |
| `DELETE` | `/api/admin/editors/{se_user_id}`     | Revoke editor privileges                     | Admin       |

### Search parameters (`GET /api/clues`)

| Parameter         | Type    | Description                          |
|-------------------|---------|--------------------------------------|
| `q`               | string  | Full-text search query               |
| `author`          | string  | ILIKE filter by author               |
| `solver`          | string  | ILIKE filter by solver               |
| `solution`        | string  | ILIKE filter by solution; wrap in double quotes for an exact, case-insensitive match |
| `date_from`       | date    | Clues on or after this date (ISO)    |
| `date_to`         | date    | Clues on or before this date (ISO)   |
| `legacy_number`   | int     | Exact legacy number                  |
| `transcript_link` | string  | Exact transcript link                |
| `order_by`        | enum    | `legacy_number`, `clue_length`, `author`, `solver`, `clue_date`, `answer_length`, `id` |
| `order_dir`       | enum    | `asc` or `desc`                      |
| `page`            | int     | Page number (1-based)                |
| `page_size`       | int     | Results per page (1–500)             |

### Example: latest 20 clues by Jafe

```text
GET /api/clues?author=Jafe&order_by=clue_date&order_dir=desc&page=1&page_size=20
```

## Web UI

The web pages are served by `app/main.py`; interactive calls go to the JSON API
with `fetch`/HTMX.

| Route                       | Page                                                        |
|-----------------------------|-------------------------------------------------------------|
| `/`                         | Home: search box + latest 20 clues                          |
| `/search`                   | Search with live results, filters, and pagination           |
| `/clue/{id}`                | Redirect (301) to the canonical `/clue/{legacy_number}.png` |
| `/clue/legacy/{n}`          | Redirect to the clue page by legacy number                  |
| `/clue/{id}/edit`           | Edit form (editors/admins)                                  |
| `/clue/{id}/review`         | Candidate-solution review queue (editors/admins)            |
| `/add`                      | Add-clue form, with transcript auto-fill (editors/admins)   |
| `/sequences`                | Sequence browser (all / author / theme / tag)               |
| `/sequences/{id}`           | Redirect to `/sequences/{id}.png`                           |
| `/user/{username}`          | Redirect to `/user/{username}.png`                          |
| `/me`                       | Redirect to the logged-in user's profile                    |
| `/stats`                    | Records, leaderboards, charts, sequence concurrency stats   |
| `/admin`                    | Editor management (admins)                                  |
| `/admin/milestones`         | Authors' 1st/50th clues in a window (admins)                |
| `/clue/{n}.png` etc.        | Content-negotiated preview (see below)                      |

## Embeddable `.png` previews

`app/api/preview_routes.py` makes `/clue/{legacy_number}.png`,
`/user/{username}.png`, and `/sequences/{id}.png` the **canonical** URLs. The
same URL serves a server-rendered PNG to image fetchers (SE chat) and the real
HTML page to browsers, chosen by the `sec-fetch-dest` header with an `Accept`
fallback. PNGs are cached for 5 minutes; HTML is `no-store`; both send
`Vary: sec-fetch-dest, Accept`. If you add a `.png` route, replicate these
headers or caches will serve the wrong variant.

Images are rendered with Pillow in `app/services/preview_renderer.py` using the
DejaVu fonts (`fonts-dejavu-core` is installed in the Dockerfile; a local machine
may not have them).

## Authentication & Permissions

### Stack Exchange OAuth2 (user login)

1. Go to [Stack Apps](https://stackapps.com/apps/oauth/register) and register a
   new app.
2. Set the OAuth domain and redirect URI to your deployment URL (e.g.
   `https://cccc.poggers.website/api/auth/callback`).
3. Copy the Client ID and Client Secret into `.env` (`SE_CLIENT_ID`,
   `SE_CLIENT_SECRET`).
4. Set `SE_KEY` to your Stack Apps API key — used for anonymous (unauthenticated)
   API requests. Authenticated users' tokens automatically take priority.

The session is an itsdangerous `URLSafeSerializer` cookie named `cccc_session`
(salt `"session"`, 7-day expiry). `UserMiddleware` (`app/main.py:157`) attaches
the user to `request.state.user`; the JSON API resolves the same cookie via the
`get_current_user` dependency.

### Permissions

- **Diamond moderators** are detected at login and become admins (and editors)
  automatically.
- **Admins** manage editors at `/admin` (or `/api/admin/editors`).
- **Editors** can create/edit clues and sequences; sequence *deletion* and
  editor management are admin-only.

> **Note:** `ROOM_OWNER_IDS` and the chat-based room-owner lookup
> (`se_chat_client.get_room_owners()`) exist but are **not wired into the
> login/permission flow**. `check_permissions` only checks diamond-moderator
> status; editor access otherwise comes from admins granting it or the seed in
> `scripts/migration_002_editors.sql`.

### Chat bot account

Chat pages on `chat.stackexchange.com` are behind Cloudflare. The app uses a bot
account with email+password authentication via the vendored
[`sechat`](https://github.com/nvua/sechat) library, which caches session cookies
to avoid repeated logins (and captcha challenges).

1. Create a SE account dedicated to bot use (or repurpose an existing one).
2. Set `SE_BOT_EMAIL` and `SE_BOT_PASSWORD` in `.env`.
3. The bot authenticates once (or loads cached cookies), then uses the
   authenticated session to fetch room owner lists (`/rooms/info/{roomID}`),
   message content via the RSS search feed
   (`/feeds/search/CCCC?room={roomID}`), and falls back to the events API
   (`/chats/{roomID}/events`) for older messages.

> **Note:** `sechat` is vendored at `./sechat` because upstream is no longer
> maintained. A local fix ensures a malformed (non-string) websocket frame can't
> kill the room-listener thread (regression test:
> `tests/test_vendored_sechat.py`). Its runtime deps (`requests`,
> `websocket-client`) are pinned in `requirements.txt`.

## Chat Ingest Bot

A chat-watching daemon automatically adds CCCC clues to the database as they are
posted in the CCCC room. It runs as its own process, independent of the web app
(see `docker-compose.yml`).

```bash
docker compose up -d ingest          # or: python -m app.services.ingest
```

### How it works

1. The daemon connects to the CCCC room using the bot account
   (`SE_BOT_EMAIL` / `SE_BOT_PASSWORD`) via `sechat`. The library runs its own
   thread; the daemon bridges its synchronous message callback to the asyncio
   loop with `asyncio.run_coroutine_threadsafe`.
2. `sechat` delivers message content as raw HTML; the daemon strips tags once
   with `strip_html()` (`app/services/ingest/accept.py`) before classifying.
3. On every message it runs the **accept rule**
   (`app/services/ingest/accept.py`):
   - **Accept** — message starts with a `CCCC` header **and** ends with an
     enumeration in parentheses (e.g. `CCCC A clue (10)`, `CCCC Something
     stacked (4, 8)`, `(4-2)`, or `(2 3)`). Ingested immediately.
   - **Near-miss** — exactly one of (header, enumeration) present. Logged at
     INFO for human review.
   - **Discard** — neither present. Silently dropped (avoids noise).
4. Accepted clues are written through `clue_service.ingest_clue`, which assigns
   the next `legacy_number`, normalizes the solution to uppercase, defaults
   `clue_date` to today, and sets the author/solver "pills".
5. Deduplication is handled by the partial unique index on `clues.message_id`;
   the bot's own messages are ignored (filtered by author id).
6. When a clue lands on an author's 1st or 50th clue, the daemon posts a
   congratulatory message back into the room.

### Human correction

If a clue was mis-formatted (or missed entirely), a human can insert it mid-chain
through the web UI. The insert service bumps the `legacy_number` of everything
after the insertion point and **recomputes the author/solver pills** for the
shifted clues so the sequence counts stay correct. Renumbering is serialized with
a transaction-scoped advisory lock and temporarily drops/recreates the partial
unique index so intermediate row states can't trip it.

### Catch-up and dedupe

- `ingest_state` (single row, id = 1) stores the last-seen message ID as a
  watermark. It is written on every ingest, but **no catch-up pass reads it
  yet** — the `message_id` dedupe index is the source of truth for correctness.
- Existing clues imported from the spreadsheet carry a `transcript_link` but no
  `message_id`. `scripts/backfill_message_ids.py` walks every such clue,
  extracts the message ID from the link, and stores it so the dedup index
  protects them too. Whole-day transcript links (e.g. `/transcript/{room}/2026/08/18`)
  are skipped.

```bash
python scripts/backfill_message_ids.py --dry-run   # preview
python scripts/backfill_message_ids.py             # apply
```

## Solution Ingest (Stage 2)

Stage 2 detects candidate solutions from the chat between a clue and the next
clue. It is **extraction-first**: the LLM is only ever asked to read a bounded
window where the answer already sits, never to solve a clue cold.

- **Windows** (`window.py`, `window_manager.py`) accumulate the messages in the
  exclusive `(A, B)` span between two clues. They are **in-memory only** and are
  lost on daemon restart, which is accepted. The closing clue's own message is
  dropped from the window.
- **Solver-identity invariant**: the author of the *next* clue is the solver of
  the current clue. `WindowManager.on_clue` sets `window.solver_user_id`;
  detection searches that user's messages first and falls back to the whole
  window only if the invariant fails.
- **Detection** (`solutions.py::detect`) returns `(candidates, llm_work)` and
  runs five tiers: (1) deterministic uppercase/mixed-case extractions, (2) MD5
  hash verification (hard proof, confidence 1.0), (3) author-reply
  confirmations, (4) direct replies to the clue, (5) wordplay residue routed to
  the LLM.
- **Confidence model**: `confidence = base_weight × enum_fit`, where `enum_fit`
  is graded (Δ=0 → 1.0, Δ=1 → 0.6, Δ=2 → 0.3, Δ≥3 → 0.1). Base weights are
  `1.0` for the free case/letter extractors and `0.6` for full-message and the
  LLM classifier. A free extractor that hits enum-fit 1.0 skips the LLM for that
  message.
- **Persistence** (`persist.py::process_window`) writes `ClueCandidate` rows
  (the editor review queue) and enqueues `PendingLlm` rows for wordplay cases,
  deduplicating on `source_message_id`.
- **LLM worker** (`llm_worker.py`) is a background asyncio task inside the
  daemon that drains `pending_llm` with exponential backoff and a simple circuit
  breaker. Work is DB-backed, so an LLM outage never loses it. It uses an
  OpenAI-compatible endpoint (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`); if
  unconfigured the worker is a no-op.
- **Review** (`app/api/review.py` and `/clue/{id}/review`) is editor/admin only.
  Approving a candidate writes the solution through `update_clue`, marks the
  candidate approved, and prunes the clue's other candidates to the top 2 by
  confidence.

## Sequences

Themes, author runs, and loose tags are the same underlying thing: a `Sequence`
row joined many-to-many to clues. `seq_type` is `author`, `theme`, or `tag`; a
clue can belong to any number of sequences. Reads are public, writes require
editor privileges, and deletion is admin-only. Author sequences have an
`author` field and support
`POST /api/sequences/{id}/clues/auto-by-author` to add that author's `count`
most recent clues (skipping ones already present).

## Scripts

Each script inserts the repo root into `sys.path`, so it can be run from anywhere.

```bash
python scripts/import_csv.py [--csv FILE] [--limit N] [--if-empty]
python scripts/import_sequences.py [--csv FILE] [--limit N]
python scripts/backfill_message_ids.py [--dry-run] [--limit N]
python scripts/backfill_pills.py          # recompute author/solver pills
python backfill.py                        # one-off recovery (hard-coded dates)
```

- `import_csv.py` parses the Google Sheet export (12 metadata rows, then a header
  at row 13) and upserts by `legacy_number`. With no `--csv` it downloads the
  sheet from a hard-coded URL. `--if-empty` makes it skip when clues exist.
- `import_sequences.py` imports the theme/author columns and calls
  `Base.metadata.create_all`.
- `backfill_message_ids.py` extracts `message_id` from `transcript_link`.
- `backfill_pills.py` recomputes `clues_by_author_so_far` /
  `clues_by_solver_so_far` (repairs data from an old off-by-one).
- `backfill.py` at the repo root is a one-off recovery script with no CLI
  arguments and hard-coded date ranges; prefer the `scripts/` tools.

## Database & Migrations

- `DATABASE_URL` (asyncpg) is used by the app; `DATABASE_URL_SYNC` (psycopg2) by
  scripts and migrations.
- Migrations are plain SQL files named `scripts/migration_*.sql` and are applied
  at **web-app startup**. `app/main.py` holds a **hard-coded ordered list** of
  filenames (`app/main.py:128`). If you add a migration you MUST add its filename
  to that list or it will never run.
- `scripts/import_csv.py` auto-discovers migrations with
  `sorted(glob("migration_*.sql"))`; `scripts/import_sequences.py` just calls
  `Base.metadata.create_all`.
- Migrations run on **every** startup, so they must be **idempotent**
  (`CREATE ... IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, ...).
- The SQL splitter (`_split_sql`, duplicated in `app/main.py:92` and
  `scripts/import_csv.py:57`) understands `$$` dollar-quoting but strips any line
  beginning with `--` and splits on semicolons. A migration's SQL comments must
  therefore not contain a semicolon (see the NOTE in
  `scripts/migration_008_ingest.sql`).
- `search_vector` is a `TSVECTOR` column (deferred) maintained by a DB trigger —
  never assign to it from Python.
- `clue_length` and `answer_length` are Postgres **generated columns**
  (`migration_003`) — never assign to them from Python.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest            # run from the repo root
```

- Pure-unit tests (no DB): `test_accept.py`, `test_window_manager.py`,
  `test_solutions.py`, `test_llm_prompt.py`, `test_transcript_parser.py`,
  `test_milestone.py`, `test_vendored_sechat.py`.
- DB-backed async tests: `test_ingest_date.py`. They catch connection errors and
  `pytest.skip()` when Postgres is unreachable, so the unit suite runs without a
  database. They use `pytest.mark.asyncio`, which requires `pytest-asyncio` to
  be installed (it is not pinned in `requirements-dev.txt`).
- There is no shared fixture/conftest; tests construct objects directly and use
  `SimpleNamespace`/small fakes to avoid network/DB.

There is **no** lint config, formatter config, CI workflow, Makefile,
`pyproject.toml`, `pytest.ini`, `setup.cfg`, or `conftest.py` in this repo.

## Project Structure

```text
pse_cccc_db/
├── app/
│   ├── api/
│   │   ├── admin.py            # Editor management API
│   │   ├── auth.py             # SE OAuth2 routes + permission checks
│   │   ├── clues.py            # Clue CRUD, search, stats, CSV export
│   │   ├── preview_routes.py   # Content-negotiated .png preview routes
│   │   ├── review.py           # Candidate-solution review API
│   │   ├── sequences.py        # Sequence CRUD + membership
│   │   └── transcript.py       # Transcript parser API
│   ├── core/
│   │   └── config.py           # Settings from env vars
│   ├── db/
│   │   └── session.py          # Async SQLAlchemy engine/session
│   ├── models/
│   │   ├── clue.py             # Clue, User, ClueEditHistory, IngestState
│   │   ├── sequence.py         # Sequence + clue_sequences association
│   │   └── solution.py         # ClueCandidate, PendingLlm
│   ├── schemas/
│   │   ├── clue.py             # Clue/user/stats/transcript schemas
│   │   └── sequence.py         # Sequence schemas
│   ├── services/
│   │   ├── clue_service.py     # Shared clue write path (API + ingest daemon)
│   │   ├── preview_renderer.py # Pillow PNG rendering
│   │   ├── se_chat_client.py   # Authenticated SE chat client (sechat + cookies)
│   │   ├── transcript_parser.py# Transcript link -> clue data (RSS + events API)
│   │   └── ingest/
│   │       ├── accept.py          # CCCC header + enumeration accept rule
│   │       ├── daemon.py          # sechat watcher + milestone announcements
│   │       ├── llm_worker.py      # Background pending_llm drainer
│   │       ├── persist.py         # Window -> ClueCandidate / PendingLlm
│   │       ├── solutions.py       # Extraction-first detection pipeline
│   │       ├── state.py           # DB-persisted watermark
│   │       ├── window.py          # In-memory (A, B) window buffer
│   │       ├── window_manager.py  # Opens/closes windows on clues
│   │       └── __main__.py        # `python -m app.services.ingest` entrypoint
│   ├── static/style.css        # Dark theme CSS
│   ├── templates/              # Jinja2 pages + HTMX partials
│   └── main.py                 # FastAPI app + web routes + migrations
├── scripts/
│   ├── import_csv.py           # Clue CSV import
│   ├── import_sequences.py     # Theme/author sequence import
│   ├── backfill_message_ids.py # Fill message_id from transcript links
│   ├── backfill_pills.py       # Recompute author/solver pills
│   └── migration_001..012_*.sql# Idempotent SQL migrations
├── sechat/                     # Vendored SE chat library (patched)
├── tests/                      # pytest suite
├── backfill.py                 # One-off chat-search recovery script
├── docker-compose.yml
├── Dockerfile
├── requirements.txt / requirements-dev.txt
├── .env.example
└── README.md
```

## License

- **Website code**: [GPL v3](LICENSE) — the source code, templates, CSS, and
  scripts.
- **Clue content**: [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
  — the clue text, solutions, and explanations are contributed by Puzzling SE
  users and are not included in the source repository.