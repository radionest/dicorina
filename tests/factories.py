"""Canned pydicom Datasets for tests."""

from __future__ import annotations

from pydicom import Dataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid


def make_instance(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    *,
    patient_id: str = "P1",
    patient_name: str = "DOE^JOHN",
    modality: str = "MR",
    rows: int = 4,
    columns: int = 4,
) -> Dataset:
    """Build a minimal but C-STORE-able MR instance with PixelData."""
    ds = Dataset()
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = MRImageStorage
    ds.Modality = modality
    ds.SeriesDescription = "test series"
    ds.StudyDescription = "test study"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.Rows = rows
    ds.Columns = columns
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(rows * columns)
    ds.SpecificCharacterSet = "ISO_IR 192"

    ds.file_meta = Dataset()
    ds.file_meta.MediaStorageSOPClassUID = MRImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.ImplementationClassUID = generate_uid()
    return ds
