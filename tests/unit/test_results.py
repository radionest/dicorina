from dimsechord import SeriesResult, StudyResult

from dicorina.dimse_face.results import series_to_dataset, study_to_dataset


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
