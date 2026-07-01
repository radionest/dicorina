from __future__ import annotations

import logging

from pydicom.dataset import Dataset

from dicorina.dimse_face.face import DimseFace


class _FakeClient:
    def find_studies(self, *_args: object, **_kwargs: object) -> object:
        return "coroutine-sentinel"


class _FakeEvent:
    def __init__(self, identifier: Dataset) -> None:
        self.identifier = identifier
        self.is_cancelled = False


def _face() -> DimseFace:
    return DimseFace(
        engine=None,  # type: ignore[arg-type]
        client=_FakeClient(),  # type: ignore[arg-type]
        pacs=None,  # type: ignore[arg-type]
        allowlist=None,  # type: ignore[arg-type]
        loop=None,  # type: ignore[arg-type]
        called_aets=["DICORINA"],
    )


def test_on_find_timeout_logs_informative_message(caplog) -> None:
    """A bare TimeoutError must not produce an empty 'C-FIND failed:' log line."""
    face = _face()

    def _raise(_coro: object) -> object:
        raise TimeoutError  # empty str() — the production failure mode

    face._run = _raise  # type: ignore[method-assign]

    ident = Dataset()
    ident.QueryRetrieveLevel = "STUDY"
    ident.StudyInstanceUID = "1.2.3.4"

    with caplog.at_level(logging.ERROR, logger="dicorina.dimse_face.face"):
        out = list(face._on_find(_FakeEvent(ident)))

    assert out == [(0xC000, None)]
    assert caplog.records, "expected an error log record"
    msg = caplog.records[0].getMessage()
    assert "TimeoutError" in msg
    assert "1.2.3.4" in msg
    assert msg.strip() != "DIMSE C-FIND failed:"
