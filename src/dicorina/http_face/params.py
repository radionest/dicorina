"""Build raw C-FIND identifiers from QIDO-RS query params (pass-through)."""

from __future__ import annotations

import re

from fastapi import HTTPException
from pydicom import Dataset
from pydicom.datadict import dictionary_VR, tag_for_keyword
from pydicom.tag import BaseTag, Tag

_CHARSET = "ISO_IR 192"
_RESERVED = {"limit", "offset", "fuzzymatching", "includefield"}
_HEX_TAG = re.compile(r"^[0-9A-Fa-f]{8}$")

# Default return keys per level — mirrors dimsechord's typed query datasets
# (_build_*_query_dataset) so pass-through responses keep the same baseline
# attribute set a standard SCP would return.
_DEFAULT_KEYS = {
    "STUDY": [
        "PatientID",
        "PatientName",
        "StudyInstanceUID",
        "StudyDate",
        "StudyTime",
        "StudyDescription",
        "AccessionNumber",
        "ModalitiesInStudy",
        "NumberOfStudyRelatedSeries",
        "NumberOfStudyRelatedInstances",
        "PatientBirthDate",
        "PatientSex",
        "StudyID",
        "ReferringPhysicianName",
        "InstitutionName",
        "StationName",
        "SOPClassesInStudy",
    ],
    "SERIES": [
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "SeriesNumber",
        "Modality",
        "SeriesDescription",
        "NumberOfSeriesRelatedInstances",
        "BodyPartExamined",
        "ProtocolName",
        "SeriesDate",
        "OperatorsName",
        "PerformedProcedureStepDescription",
    ],
    "IMAGE": [
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "SOPInstanceUID",
        "SOPClassUID",
        "InstanceNumber",
        "Rows",
        "Columns",
        "ImageType",
        "ContentDate",
        "SliceThickness",
    ],
}


def _resolve_tag(name: str) -> BaseTag:
    if _HEX_TAG.match(name):
        return Tag(int(name, 16))
    tag = tag_for_keyword(name)
    if tag is None:
        raise HTTPException(status_code=400, detail=f"Unknown DICOM attribute: {name}")
    return Tag(tag)


def _set_element(ds: Dataset, tag: BaseTag, value: str) -> None:
    try:
        vr = dictionary_VR(tag)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown DICOM tag: {tag}") from None
    ds.add_new(tag, vr, value)


def build_identifier(
    level: str,
    params: dict[str, str],
    includefields: list[str] | None = None,
    *,
    study_uid: str | None = None,
    series_uid: str | None = None,
) -> Dataset:
    """Map QIDO params onto a raw C-FIND identifier (any keyword or GGGGEEEE)."""
    ds = Dataset()
    ds.SpecificCharacterSet = _CHARSET
    ds.QueryRetrieveLevel = level
    for keyword in _DEFAULT_KEYS[level]:
        setattr(ds, keyword, "")
    for name, value in params.items():
        if name in _RESERVED:
            continue
        _set_element(ds, _resolve_tag(name), value)
    for field in includefields or []:
        for part in field.split(","):
            part = part.strip()
            if not part or part.lower() == "all":
                continue
            tag = _resolve_tag(part)
            if tag not in ds:
                _set_element(ds, tag, "")
    if study_uid is not None:
        ds.StudyInstanceUID = study_uid
    if series_uid is not None:
        ds.SeriesInstanceUID = series_uid
    return ds


def pagination(params: dict[str, str]) -> tuple[int | None, int]:
    """Extract QIDO ``limit``/``offset`` (applied locally to the stream)."""

    def _int(name: str) -> int | None:
        raw = params.get(name)
        if raw is None:
            return None
        try:
            val = int(raw)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"{name} must be an integer"
            ) from None
        if val < 0:
            raise HTTPException(status_code=400, detail=f"{name} must be >= 0")
        return val

    return _int("limit"), _int("offset") or 0
