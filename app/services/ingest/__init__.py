"""Chat ingest daemon package.

The daemon watches the CCCC chat room via the sechat library's message
callback and automatically adds valid clues to the database.

Layout:
  - accept.py   — the accept/discard rule (header + enumeration)
  - state.py    — the DB-persisted watermark (recovery after disconnect)
  - daemon.py   — the sechat callback wiring + ingest orchestration
  - __main__.py — entrypoint (`python -m app.services.ingest`)
"""
