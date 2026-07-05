"""Unit tests for DimseFace._on_find: it must honour cfind_timeout and release
its upstream find lease deterministically on SCU disconnect -- the DIMSE-side
siblings of ProxyService's HTTP pass-through (see tests/unit/test_service.py)."""

from __future__ import annotations

import gc
from types import SimpleNamespace
from typing import Any

from pydicom import Dataset
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    StudyRootQueryRetrieveInformationModelFind,
)

from dicorina.dimse_face.face import DimseFace
from tests.factories import make_instance


def _event(identifier: Dataset, model: object) -> Any:
    return SimpleNamespace(
        identifier=identifier,
        is_cancelled=False,
        context=SimpleNamespace(abstract_syntax=model),
    )


def _face(query: Any, *, cfind_timeout: float = 30.0) -> DimseFace:
    none: Any = None
    return DimseFace(none, none, query, none, none, none, "DICORINA", cfind_timeout=cfind_timeout)


def test_on_find_passes_cfind_timeout() -> None:
    """The pass-through C-FIND must apply the configured cfind timeout, like the
    QIDO path -- otherwise a hung PACS pins a pynetdicom worker thread until
    iter_find's internal default rather than cfg.timeouts.cfind."""
    captured: dict[str, object] = {}

    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        captured["timeout"] = timeout
        yield make_instance("1.1", "1.2", "1.3")

    face = _face(SimpleNamespace(iter_find=fake_iter_find), cfind_timeout=17.0)
    list(face._on_find(_event(Dataset(), StudyRootQueryRetrieveInformationModelFind)))

    assert captured["timeout"] == 17.0


def test_on_find_closes_upstream_deterministically() -> None:
    """On SCU abort/disconnect (GeneratorExit into the paused handler) the upstream
    iter_find generator must be closed via try/finally, not left to GC: a genuine
    reference cycle (as in pynetdicom Association objects) leaves refcounting-based
    cleanup arbitrarily delayed, so without the fix a dropped C-FIND would not
    release its find lease -- exhausting the per-AET find pool."""
    closed: list[str] = []

    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        container: list = []

        def gen():
            _keep_cycle_alive = container  # frame -> container
            try:
                for i in range(3):
                    yield make_instance(f"1.{i}", f"2.{i}", f"3.{i}")
            finally:
                closed.append("closed")

        g = gen()
        container.append(g)  # container -> generator, closing the cycle
        return g

    face = _face(SimpleNamespace(iter_find=fake_iter_find))
    handler: Any = face._on_find(_event(Dataset(), StudyRootQueryRetrieveInformationModelFind))

    gc.disable()
    try:
        next(handler)  # consume the first (0xFF00, ds); upstream mid-iteration
        handler.close()  # SCU disconnect → GeneratorExit into the paused handler
        assert closed == ["closed"]
    finally:
        gc.enable()
