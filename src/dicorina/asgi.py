"""Module-level ASGI app for `uvicorn dicorina.asgi:app`."""

from __future__ import annotations

import os

from dicorina.app import create_app
from dicorina.config import load_config
from dicorina.logging_setup import configure_logging

_config = load_config(os.environ["DICORINA_CONFIG"])
# Re-applied here because uvicorn runs its own dictConfig between
# ``__main__.main`` and importing this module.
configure_logging(_config.logging)
app = create_app(_config)
