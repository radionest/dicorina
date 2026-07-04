import pytest
from fastapi import HTTPException

from dicorina.http_face.params import build_identifier, pagination


def test_defaults_and_level() -> None:
    ds = build_identifier("STUDY", {})
    assert str(ds.QueryRetrieveLevel) == "STUDY"
    assert str(ds.SpecificCharacterSet) == "ISO_IR 192"
    assert ds["PatientName"].VM == 0  # default return key requested (zero-length)
    assert ds["NumberOfStudyRelatedInstances"].VM == 0


def test_keyword_and_hex_params_become_elements() -> None:
    ds = build_identifier("STUDY", {"PatientName": "Иванов*", "00080080": "HOSP"})
    assert str(ds.PatientName) == "Иванов*"
    assert str(ds.InstitutionName) == "HOSP"


def test_unknown_keyword_rejected() -> None:
    with pytest.raises(HTTPException) as ei:
        build_identifier("STUDY", {"NotADicomTag": "x"})
    assert ei.value.status_code == 400


def test_includefield_csv_adds_empty_tags() -> None:
    ds = build_identifier("STUDY", {}, includefields=["StationName,00081030"])
    assert ds["StationName"].VM == 0
    assert ds["StudyDescription"].VM == 0


def test_includefield_all_ignored() -> None:
    ds = build_identifier("STUDY", {}, includefields=["all"])
    assert str(ds.QueryRetrieveLevel) == "STUDY"


def test_reserved_params_not_forwarded() -> None:
    ds = build_identifier("STUDY", {"limit": "5", "offset": "2", "fuzzymatching": "true"})
    assert "limit" not in [e.keyword for e in ds]


def test_path_uids_override_params() -> None:
    ds = build_identifier(
        "SERIES", {"StudyInstanceUID": "9.9"}, study_uid="1.2", series_uid=None
    )
    assert str(ds.StudyInstanceUID) == "1.2"
    assert str(ds.QueryRetrieveLevel) == "SERIES"


def test_image_level_gets_both_path_uids() -> None:
    ds = build_identifier("IMAGE", {}, study_uid="1.2", series_uid="1.2.3")
    assert str(ds.StudyInstanceUID) == "1.2"
    assert str(ds.SeriesInstanceUID) == "1.2.3"
    assert ds["SOPInstanceUID"].VM == 0


def test_numeric_vr_value_coerced() -> None:
    ds = build_identifier("IMAGE", {"Rows": "512"}, study_uid="1.2", series_uid="1.2.3")
    assert ds["Rows"].value == 512


def test_numeric_vr_invalid_value_rejected() -> None:
    with pytest.raises(HTTPException) as ei:
        build_identifier("IMAGE", {"Rows": "abc"}, study_uid="1.2", series_uid="1.2.3")
    assert ei.value.status_code == 400


def test_binary_vr_key_rejected() -> None:
    with pytest.raises(HTTPException) as ei:
        build_identifier("STUDY", {"7FE00010": "x"})  # PixelData, VR OB/OW
    assert ei.value.status_code == 400


def test_includefield_numeric_vr_is_zero_length() -> None:
    ds = build_identifier("STUDY", {}, includefields=["Rows"])
    assert ds["Rows"].VM == 0


def test_pagination() -> None:
    assert pagination({}) == (None, 0)
    assert pagination({"limit": "2", "offset": "3"}) == (2, 3)
    with pytest.raises(HTTPException):
        pagination({"limit": "abc"})
    with pytest.raises(HTTPException):
        pagination({"offset": "-1"})
