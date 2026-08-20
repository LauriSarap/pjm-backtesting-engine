"""`python -m viz_server` entry point.

Boots the FastAPI app on 127.0.0.1:8765 by default. Env overrides:

    VIZ_SERVER_HOST   bind host                   (default 127.0.0.1)
    VIZ_SERVER_PORT   bind port                   (default 8765)
    VIZ_SERVER_DEV    "1" enables uvicorn auto-reload watching viz_server/
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn


def main() -> None:
    host = os.environ.get("VIZ_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("VIZ_SERVER_PORT", "8765"))
    dev = os.environ.get("VIZ_SERVER_DEV") == "1"

    kwargs: dict = {
        "host": host,
        "port": port,
        "log_level": "info",
    }
    if dev:
        # Watch only the viz_server source dir — engine + parquet cache stay
        # outside the watch tree so loader-level changes don't trigger a
        # full preload thrash.
        kwargs["reload"] = True
        kwargs["reload_dirs"] = [str(Path(__file__).resolve().parent)]

    uvicorn.run("viz_server.api:app", **kwargs)


if __name__ == "__main__":
    main()
