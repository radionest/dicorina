"""Module-level ASGI app for `uvicorn dicorina.asgi:app`."""

from __future__ import annotations

import os

from dicorina.app import create_app
from dicorina.config import load_config

app = create_app(load_config(os.environ["DICORINA_CONFIG"]))
