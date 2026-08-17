"""Entrypoint for the chat ingest daemon.

Run with:  python -m app.services.ingest
"""

from app.services.ingest.daemon import main

if __name__ == "__main__":
    main()
