from __future__ import annotations

import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import pytest
from pydicom.uid import generate_uid

from tests.factories import make_instance
from tests.fake_pacs import FakePacs


@pytest.fixture
def free_port() -> Callable[[], int]:
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    return _free_port


@pytest.fixture
def seeded_study() -> dict[str, list[str]]:
    study = generate_uid()
    s1, s2 = generate_uid(), generate_uid()
    return {
        "study": [study],
        "series": [s1, s2],
        s1: [generate_uid(), generate_uid()],
        s2: [generate_uid(), generate_uid()],
    }


@pytest.fixture
def fake_pacs(
    free_port: Callable[[], int], seeded_study: dict[str, list[str]]
) -> Iterator[FakePacs]:
    pacs = FakePacs(aet="FAKEPACS")
    study = seeded_study["study"][0]
    for series in seeded_study["series"]:
        for sop in seeded_study[series]:
            pacs.add_instance(make_instance(study, series, sop))
    port = free_port()
    pacs.start(port)
    pacs.port = port  # type: ignore[attr-defined]
    try:
        yield pacs
    finally:
        pacs.stop()
