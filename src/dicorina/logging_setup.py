"""Process-wide logging setup — without it dicorina's journal is nearly empty.

uvicorn's ``LOGGING_CONFIG`` attaches handlers to the ``uvicorn*`` loggers only
and leaves the root logger with none, so every record from ``dicorina``,
``dimsechord`` and ``pynetdicom`` falls through to ``logging.lastResort`` — a
WARNING-level stderr handler. Everything logged below WARNING is discarded
before it reaches journald, which is why an overloaded proxy can refuse
associations in silence: pynetdicom logs "Rejecting Association" at INFO.

This module installs the missing root handler and sets the per-library levels.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dicorina.config import LoggingConfig

_HANDLER_NAME = "dicorina"

# threadName is load-bearing here: pynetdicom runs one thread per inbound
# association, so it is what ties a stalled operation to the association
# holding the slot.
FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"


def configure_logging(cfg: LoggingConfig) -> None:
    """Install the root stderr handler and set the dicorina/dimsechord/pynetdicom levels.

    Idempotent: both entrypoints call it — ``dicorina.__main__`` before uvicorn
    starts, ``dicorina.asgi`` at import time (i.e. after uvicorn has run its own
    ``dictConfig``) — and the second call replaces the handler rather than
    adding a duplicate.
    """
    root = logging.getLogger()
    for installed in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(installed)
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)

    # The root level stays at its WARNING default so third-party chatter
    # (pydicom above all) is not promoted along with ours: a propagated record
    # is filtered by the level of the logger that emitted it, not by root's.
    logging.getLogger("dicorina").setLevel(cfg.level)
    logging.getLogger("dimsechord").setLevel(cfg.level)
    logging.getLogger("pynetdicom").setLevel(cfg.pynetdicom_level)
