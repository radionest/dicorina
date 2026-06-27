from dicorina.http_face.params import (
    image_query_from_params,
    series_query_from_params,
    study_query_from_params,
)


def test_study_keyword_params_map() -> None:
    q = study_query_from_params({"PatientID": "P1", "ModalitiesInStudy": "CT"})
    assert q.patient_id == "P1"
    assert q.modality == "CT"


def test_study_tag_params_map() -> None:
    q = study_query_from_params({"00100020": "P9"})
    assert q.patient_id == "P9"


def test_non_dicom_params_ignored() -> None:
    q = study_query_from_params({"limit": "10", "includefield": "all", "offset": "0"})
    assert q.patient_id is None and q.study_instance_uid is None


def test_series_query_requires_study() -> None:
    q = series_query_from_params("1.2.3", {"Modality": "MR"})
    assert q.study_instance_uid == "1.2.3"
    assert q.modality == "MR"


def test_image_query() -> None:
    q = image_query_from_params("1.2.3", "1.2.3.4", {"SOPInstanceUID": "1.2.3.4.5"})
    assert q.sop_instance_uid == "1.2.3.4.5"
