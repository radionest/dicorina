"""Unit tests for the C-STORE relay handlers: per-association StoreSession
lifecycle and status mapping (spec: dimse-store-relay)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from dimsechord import AssociationError, NoPresentationContextError
from pydicom.uid import generate_uid

import dicorina.dimse_face.face as face_mod
from dicorina.dimse_face.face import DimseFace
from tests.factories import make_instance


class _FakeSession:
    instances: ClassVar[list[_FakeSession]] = []

    def __init__(self, peer: Any, *, calling_aet: str, timeout: float) -> None:
        self.peer = peer
        self.calling_aet = calling_aet
        self.timeout = timeout
        self.stored: list[Any] = []
        self.closed = False
        self.status = 0x0000
        self.raise_exc: Exception | None = None
        _FakeSession.instances.append(self)

    def store(self, dataset: Any) -> int:
        if self.raise_exc is not None:
            raise self.raise_exc
        self.stored.append(dataset)
        return self.status

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_session(monkeypatch):
    _FakeSession.instances = []
    monkeypatch.setattr(face_mod, "StoreSession", _FakeSession)


def _face(**kwargs: Any) -> DimseFace:
    none: Any = None
    return DimseFace(none, none, none, none, none, none, "DICORINA", **kwargs)


def _store_event(assoc: object) -> Any:
    ds = make_instance(generate_uid(), generate_uid(), generate_uid())
    meta = ds.file_meta
    return SimpleNamespace(assoc=assoc, dataset=ds, file_meta=meta)


def _end_event(assoc: object) -> Any:
    return SimpleNamespace(assoc=assoc)


def test_session_created_lazily_per_association() -> None:
    face = _face()
    a1, a2 = object(), object()
    assert face._on_store(_store_event(a1)) == 0x0000
    assert face._on_store(_store_event(a1)) == 0x0000
    assert len(_FakeSession.instances) == 1
    assert len(_FakeSession.instances[0].stored) == 2
    face._on_store(_store_event(a2))
    assert len(_FakeSession.instances) == 2


def test_dataset_carries_file_meta() -> None:
    face = _face()
    face._on_store(_store_event(object()))
    ds = _FakeSession.instances[0].stored[0]
    assert str(ds.file_meta.TransferSyntaxUID)


def test_status_passes_through_verbatim() -> None:
    face = _face()
    a = object()
    face._on_store(_store_event(a))
    _FakeSession.instances[0].status = 0xB000
    assert face._on_store(_store_event(a)) == 0xB000


def test_no_context_maps_to_0122_and_session_survives() -> None:
    face = _face()
    a = object()
    face._on_store(_store_event(a))
    session = _FakeSession.instances[0]
    session.raise_exc = NoPresentationContextError("1.2.840.10008.5.1.4.1.1.4", "1.2")
    assert face._on_store(_store_event(a)) == 0x0122
    session.raise_exc = None
    assert face._on_store(_store_event(a)) == 0x0000
    assert len(_FakeSession.instances) == 1  # same session throughout


def test_association_error_maps_to_a700() -> None:
    face = _face()
    a = object()
    face._on_store(_store_event(a))
    _FakeSession.instances[0].raise_exc = AssociationError("upstream down")
    assert face._on_store(_store_event(a)) == 0xA700


def test_unexpected_error_maps_to_c000() -> None:
    face = _face()
    a = object()
    face._on_store(_store_event(a))
    _FakeSession.instances[0].raise_exc = RuntimeError("boom")
    assert face._on_store(_store_event(a)) == 0xC000


def test_assoc_end_closes_and_pops() -> None:
    face = _face()
    a = object()
    face._on_store(_store_event(a))
    face._on_assoc_end(_end_event(a))
    assert _FakeSession.instances[0].closed
    face._on_assoc_end(_end_event(a))  # second end event: no-op
    face._on_store(_store_event(a))  # store after end -> fresh session
    assert len(_FakeSession.instances) == 2


def test_assoc_end_without_session_is_noop() -> None:
    face = _face()
    face._on_assoc_end(_end_event(object()))  # must not raise


def test_stop_closes_leftover_sessions() -> None:
    face = _face()
    face._on_store(_store_event(object()))
    face.stop()
    assert _FakeSession.instances[0].closed


def test_configured_identity_and_timeout() -> None:
    face = _face(store_aet="DICSTORE", store_timeout=7.0)
    face._on_store(_store_event(object()))
    assert _FakeSession.instances[0].calling_aet == "DICSTORE"
    assert _FakeSession.instances[0].timeout == 7.0


def test_identity_falls_back_to_face_aet() -> None:
    face = _face()
    face._on_store(_store_event(object()))
    assert _FakeSession.instances[0].calling_aet == "DICORINA"


def test_conn_close_during_store_defers_cleanup(monkeypatch) -> None:
    # EVT_CONN_CLOSE runs on pynetdicom's DUL thread and can land while
    # _on_store is still mid-store on the reactor thread. Model that
    # interleaving deterministically: the fake session's store() itself
    # calls _on_assoc_end for the same assoc, synchronously, before
    # returning. The session must NOT be closed at that point (the
    # in-flight marker defers it) — only once _on_store's own finally
    # runs, after store() has actually returned.
    face = _face()
    a = object()
    closed_during_store: list[bool] = []

    class _RacingSession(_FakeSession):
        def store(self, dataset: Any) -> int:
            face._on_assoc_end(_end_event(a))
            closed_during_store.append(self.closed)
            return super().store(dataset)

    monkeypatch.setattr(face_mod, "StoreSession", _RacingSession)

    assert face._on_store(_store_event(a)) == 0x0000
    assert closed_during_store == [False]  # not closed while store() was still running
    assert _FakeSession.instances[0].closed  # closed once _on_store's finally ran
    assert face._store_sessions == {}  # registry entry dropped, not left dangling

    face._on_store(_store_event(a))
    assert len(_FakeSession.instances) == 2  # registry entry was dropped -> fresh session
