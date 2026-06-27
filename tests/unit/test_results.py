from dimsechord import ImageResult, SeriesResult, StudyResult

from dicorina.dimse_face.results import image_to_dataset, series_to_dataset, study_to_dataset


def test_study_to_dataset_sets_charset_and_fields() -> None:
    r = StudyResult(
        study_instance_uid="1.2.3",
        patient_id="P1",
        patient_name="Иванов^Пётр",
        modalities_in_study="CT\\SR",
        number_of_study_related_instances=7,
    )
    ds = study_to_dataset(r)
    assert ds.SpecificCharacterSet == "ISO_IR 192"
    assert ds.QueryRetrieveLevel == "STUDY"
    assert ds.StudyInstanceUID == "1.2.3"
    assert str(ds.PatientName) == "Иванов^Пётр"
    assert list(ds.ModalitiesInStudy) == ["CT", "SR"]
    assert ds.NumberOfStudyRelatedInstances == 7


def test_series_to_dataset_minimal() -> None:
    r = SeriesResult(study_instance_uid="1.2.3", series_instance_uid="1.2.3.4", modality="MR")
    ds = series_to_dataset(r)
    assert ds.QueryRetrieveLevel == "SERIES"
    assert ds.SeriesInstanceUID == "1.2.3.4"
    assert ds.Modality == "MR"


def test_image_to_dataset() -> None:
    r = ImageResult(
        study_instance_uid="1.2.3",
        series_instance_uid="1.2.3.4",
        sop_instance_uid="1.2.3.4.5",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.4",
        instance_number=3,
        rows=256,
        columns=256,
    )
    ds = image_to_dataset(r)
    assert ds.SpecificCharacterSet == "ISO_IR 192"
    assert ds.QueryRetrieveLevel == "IMAGE"
    assert ds.SOPInstanceUID == "1.2.3.4.5"
    assert ds.Rows == 256
    assert ds.Columns == 256
