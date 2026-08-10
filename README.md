# CCCC DB

A replacement for the Google Sheets archive of the **Cryptic Clue Chat Chains**
(CCCC), an informal competition in [The Sphinx's Lair](https://chat.stackexchange.com/rooms/14524)
on Puzzling Stack Exchange.

## Features

- **PostgreSQL-backed** with full-text search across clue text, solution, explanation, author, and solver
- **Stack Exchange OAuth2** authentication — log in with your SE account
- **Room-owner authorization** — only authorised room owners can add/edit clues
- **REST API** with automatic OpenAPI/Swagger docs — designed for a future bot integration
- **Transcript link parser** — paste a `chat.stackexchange.com/transcript/...` URL and auto-fill the clue entry form
- **HTMX web UI** — live search, pagination, clue detail, stats — no SPA build step
- **CSV import** — bulk import the existing ~9,600 clues from the Google Sheet export

## Tech Stack

| Layer       | Technology           | Why                                          |
|-------------|----------------------|----------------------------------------------|
| Database    | PostgreSQL 16        | Full-text search (tsvector + GIN), concurrent access |
| Backend     | Python 3.12 + FastAPI| Automatic OpenAPI docs, async, Pydantic validation |
| Frontend    | HTMX + Jinja2        | Interactive UI without SPA complexity         |
| Auth        | Stack Exchange OAuth2| SE-native, verifies room-owner status         |

## Quick Start (Docker)

```bash
# 1. Clone
git clone https://github.com/themoonisacheese/pse_cccc_db.git
cd pse_cccc_db

# 2. Configure
cp .env.example .env
# Edit .env — set SECRET_KEY and (optionally) SE OAuth credentials

# 3. Run
docker compose up --build
```

This starts PostgreSQL and the app on http://localhost:8000.
The first run imports 100 clues from the CSV as a test.
To import all clues, change the import command in `docker-compose.yml` or run:

```bash
docker compose exec app python scripts/import_csv.py
```

## Local Development (without Docker)

```bash
# 1. Install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install beautifulsoup4  # for transcript parsing

# 2. Start PostgreSQL (or use a local instance)
#    Create database: createdb cccc

# 3. Configure
cp .env.example .env
# Edit DATABASE_URL to point to your local PostgreSQL

# 4. Import data
python scripts/import_csv.py --limit 100  # test with 100 clues
# python scripts/import_csv.py            # full import (all ~9600)

# 5. Run
uvicorn app.main:app --reload
```

## REST API

The API is at `/api` with interactive docs at `/api/docs` (Swagger UI).

| Method   | Endpoint                     | Description                          | Auth           |
|----------|------------------------------|--------------------------------------|----------------|
| `GET`    | `/api/clues`                 | Search/filter/paginate clues         | Public         |
| `GET`    | `/api/clues/{id}`            | Get a single clue by DB ID           | Public         |
| `POST`   | `/api/clues`                 | Create a new clue                    | Room owner     |
| `PUT`    | `/api/clues/{id}`            | Update an existing clue              | Room owner     |
| `DELETE` | `/api/clues/{id}`            | Delete a clue                        | Admin          |
| `GET`    | `/api/clues/stats/overview`  | Aggregate stats                      | Public         |
| `GET`    | `/api/clues/stats/authors`   | Author leaderboard                   | Public         |
| `GET`    | `/api/clues/stats/solvers`   | Solver leaderboard                   | Public         |
| `GET`    | `/api/transcript/parse?url=` | Parse a transcript link             | Public         |
| `GET`    | `/api/auth/login`            | Initiate SE OAuth2 flow             | Public         |
| `GET`    | `/api/auth/callback`         | OAuth2 callback                     | Public         |
| `GET`    | `/api/auth/me`               | Current user info                   | Session        |

### Search Parameters (GET /api/clues)

| Parameter         | Type    | Description                          |
|-------------------|---------|--------------------------------------|
| `q`               | string  | Full-text search query               |
| `author`          | string  | ILIKE filter by author               |
| `solver`          | string  | ILIKE filter by solver               |
| `solution`        | string  | ILIKE filter by solution             |
| `date_from`       | date    | Clues on or after this date          |
| `date_to`         | date    | Clues on or before this date         |
| `legacy_number`   | int     | Exact legacy number                  |
| `transcript_link` | string  | Exact transcript link               |
| `order_by`        | enum    | `legacy_number`, `clue_length`, `author`, `solver`, `clue_date`, `answer_length`, `id` |
| `order_dir`       | enum    | `asc` or `desc`                      |
| `page`            | int     | Page number (1-based)                |
| `page_size`       | int     | Results per page (1–500)             |

### Example: Latest 20 clues by Jafe

```
GET /api/clues?author=Jafe&order_by=clue_date&order_dir=desc&page=1&page_size=20
```

## Stack Exchange OAuth Setup

1. Go to [Stack Apps](https://stackapps.com/apps/oauth/register) and register a new app.
2. Set the redirect URI to `http://localhost:8000/auth/callback` (for local dev).
3. Copy the Client ID and Client Secret into your `.env` file.
4. Optionally add an API key for higher rate limits.
5. Set `ROOM_OWNER_IDS` to a comma-separated list of SE user IDs who should be authorised as room owners.

## Bot Integration (Future)

The REST API is designed to be used by a chat-watching bot:

- **Create a clue**: `POST /api/clues` with JSON body (requires an API token or session cookie)
- **Search clues**: `GET /api/clues?q=...&author=...&page=1`
- **Parse a transcript link**: `GET /api/transcript/parse?url=...`

The bot can use the API independently of the web UI. For bot authentication,
create a user manually in the database with `is_admin=true` and use a session
cookie or a future API token mechanism.

## Project Structure

```
pse_cccc_db/
├── app/
│   ├── api/
│   │   ├── auth.py            # SE OAuth2 routes
│   │   ├── clues.py           # CRUD + search + stats API
│   │   └── transcript.py     # Transcript parser API
│   ├── core/
│   │   └── config.py         # Settings from env vars
│   ├── db/
│   │   └── session.py        # Async SQLAlchemy engine
│   ├── models/
│   │   └── clue.py           # Clue, User, ClueEditHistory models
│   ├── schemas/
│   │   └── clue.py           # Pydantic schemas
│   ├── services/
│   │   └── transcript_parser.py  # Chat transcript HTML parser
│   ├── static/
│   │   └── style.css         # Dark theme CSS
│   ├── templates/
│   │   ├── base.html         # Layout with navbar
│   │   ├── index.html        # Home page
│   │   ├── search.html       # Search page with HTMX
│   │   ├── clue_detail.html  # Single clue view
│   │   ├── add_clue.html     # Clue entry form
│   │   ├── stats.html        # Statistics page
│   │   ├── error.html        # Error page
│   │   └── partials/
│   │       └── clue_results.html  # HTMX partial for search results
│   └── main.py               # FastAPI app + web routes
├── scripts/
│   ├── import_csv.py         # CSV import script
│   └── migration_001_fts.sql # FTS trigger migration
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## License

MIT
