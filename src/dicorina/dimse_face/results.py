"""Convert dimsechord typed C-FIND results into pydicom response Datasets (D9: UTF-8)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydicom import Dataset

if TYPE_CHECKING:
    from dimsechord import ImageResult, SeriesResult, StudyResult

_CHARSET = "ISO_IR 192"


def study_to_dataset(r: StudyResult) -> Dataset:
    ds = Dataset()
    ds.SpecificCharacterSet = _CHARSET
    ds.QueryRetrieveLevel = "STUDY"
    ds.StudyInstanceUID = r.study_instance_uid
    if r.patient_id is not None:
        ds.PatientID = r.patient_id
    if r.patient_name is not None:
        ds.PatientName = r.patient_name
    if r.study_date is not None:
        ds.StudyDate = r.study_date
    if r.study_time is not None:
        ds.StudyTime = r.study_time
    if r.study_description is not None:
        ds.StudyDescription = r.study_description
    if r.accession_number is not None:
        ds.AccessionNumber = r.accession_number
    if r.modalities_in_study is not None:
        ds.ModalitiesInStudy = r.modalities_in_study.split("\\")
    if r.number_of_study_related_series is not None:
        ds.NumberOfStudyRelatedSeries = r.number_of_study_related_series
    if r.number_of_study_related_instances is not None:
        ds.NumberOfStudyRelatedInstances = r.number_of_study_related_instances
    return ds


def series_to_dataset(r: SeriesResult) -> Dataset:
    ds = Dataset()
    ds.SpecificCharacterSet = _CHARSET
    ds.QueryRetrieveLevel = "SERIES"
    ds.StudyInstanceUID = r.study_instance_uid
    ds.SeriesInstanceUID = r.series_instance_uid
    if r.series_number is not None:
        ds.SeriesNumber = r.series_number
    if r.modality is not None:
        ds.Modality = r.modality
    if r.series_description is not None:
        ds.SeriesDescription = r.series_description
    if r.number_of_series_related_instances is not None:
        ds.NumberOfSeriesRelatedInstances = r.number_of_series_related_instances
    return ds


def image_to_dataset(r: ImageResult) -> Dataset:
    ds = Dataset()
    ds.SpecificCharacterSet = _CHARSET
    ds.QueryRetrieveLevel = "IMAGE"
    ds.StudyInstanceUID = r.study_instance_uid
    ds.SeriesInstanceUID = r.series_instance_uid
    ds.SOPInstanceUID = r.sop_instance_uid
    if r.sop_class_uid is not None:
        ds.SOPClassUID = r.sop_class_uid
    if r.instance_number is not None:
        ds.InstanceNumber = r.instance_number
    return ds
