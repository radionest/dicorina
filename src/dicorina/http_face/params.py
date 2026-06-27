"""Map QIDO-RS query params (DICOM keyword or tag) to dimsechord query models."""

from __future__ import annotations

from dimsechord import ImageQuery, SeriesQuery, StudyQuery

_STUDY_MAP = {
    "PatientID": "patient_id",
    "00100020": "patient_id",
    "PatientName": "patient_name",
    "00100010": "patient_name",
    "StudyInstanceUID": "study_instance_uid",
    "0020000D": "study_instance_uid",
    "StudyDate": "study_date",
    "00080020": "study_date",
    "StudyDescription": "study_description",
    "00081030": "study_description",
    "AccessionNumber": "accession_number",
    "00080050": "accession_number",
    "ModalitiesInStudy": "modality",
    "00080061": "modality",
}


def study_query_from_params(params: dict[str, str]) -> StudyQuery:
    fields = {dest: params[k] for k, dest in _STUDY_MAP.items() if k in params}
    return StudyQuery(**fields)


def series_query_from_params(study_uid: str, params: dict[str, str]) -> SeriesQuery:
    fields: dict[str, str] = {"study_instance_uid": study_uid}
    for src, dest in (
        ("SeriesInstanceUID", "series_instance_uid"),
        ("Modality", "modality"),
        ("SeriesNumber", "series_number"),
        ("SeriesDescription", "series_description"),
    ):
        if src in params:
            fields[dest] = params[src]
    return SeriesQuery(**fields)


def image_query_from_params(study_uid: str, series_uid: str, params: dict[str, str]) -> ImageQuery:
    fields: dict[str, str] = {
        "study_instance_uid": study_uid,
        "series_instance_uid": series_uid,
    }
    for src, dest in (
        ("SOPInstanceUID", "sop_instance_uid"),
        ("InstanceNumber", "instance_number"),
    ):
        if src in params:
            fields[dest] = params[src]
    return ImageQuery(**fields)
