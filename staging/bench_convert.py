"""Reference microbenchmark: Dataset -> DICOM JSON dict -> JSON bytes.

Not part of CI. Basis for the no-multiprocessing decision in
docs/superpowers/specs/2026-07-02-streaming-passthrough-design.md:
~69 us per QIDO result, ~326 us per full CT header (py3.14) — conversion
overlaps network arrival in a producer thread, so it is not the bottleneck.
Run: uv run python staging/bench_convert.py
"""

import json
import sys
import time

from dimsechord import dataset_to_dicom_json
from pydicom import Dataset
from pydicom.uid import generate_uid


def make_qido_ds() -> Dataset:
    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.QueryRetrieveLevel = "STUDY"
    ds.StudyInstanceUID = generate_uid()
    ds.PatientID = "PAT-000123"
    ds.PatientName = "IVANOV^IVAN^IVANOVICH"
    ds.StudyDate = "20260101"
    ds.StudyTime = "120000"
    ds.StudyDescription = "CT CHEST ABDOMEN PELVIS WITH CONTRAST"
    ds.AccessionNumber = "ACC123456"
    ds.ModalitiesInStudy = ["CT", "SR"]
    ds.NumberOfStudyRelatedSeries = "5"
    ds.NumberOfStudyRelatedInstances = "605"
    return ds


def make_header_ds() -> Dataset:
    ds = make_qido_ds()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.Modality = "CT"
    ds.SeriesNumber = "3"
    ds.InstanceNumber = "42"
    ds.Rows = 512
    ds.Columns = 512
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.RescaleIntercept = "-1024"
    ds.RescaleSlope = "1"
    ds.SliceThickness = "1.0"
    ds.KVP = "120"
    ds.ImagePositionPatient = ["-250.0", "-250.0", "-100.0"]
    ds.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
    ds.PixelSpacing = ["0.976562", "0.976562"]
    ds.WindowCenter = "40"
    ds.WindowWidth = "400"
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.SamplesPerPixel = 1
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "M"
    ds.PatientAge = "056Y"
    ds.BodyPartExamined = "CHEST"
    ds.ScanOptions = "HELICAL MODE"
    ds.ReconstructionDiameter = "500"
    ds.ConvolutionKernel = "STANDARD"
    ds.GantryDetectorTilt = "0"
    ds.TableHeight = "150"
    ds.ExposureTime = "500"
    ds.XRayTubeCurrent = "300"
    ds.Exposure = "150"
    ds.InstitutionName = "HOSPITAL"
    ds.Manufacturer = "VENDOR"
    ds.ManufacturerModelName = "SCANNER 9000"
    ds.StationName = "CT01"
    ds.SoftwareVersions = "v1.2.3"
    ds.ProtocolName = "CHEST ROUTINE"
    ds.SeriesDescription = "AXIAL 1.0"
    ds.FrameOfReferenceUID = generate_uid()
    ds.SliceLocation = "-100.0"
    ds.AcquisitionNumber = "1"
    ds.AcquisitionDate = "20260101"
    ds.AcquisitionTime = "120001"
    ds.ContentDate = "20260101"
    ds.ContentTime = "120002"
    return ds


def bench(label: str, ds: Dataset, n: int) -> None:
    base = "http://localhost:8042/dicom-web"
    for _ in range(50):
        dataset_to_dicom_json(ds, base)
    t0 = time.perf_counter()
    dicts = [dataset_to_dicom_json(ds, base) for _ in range(n)]
    t1 = time.perf_counter()
    for d in dicts:
        json.dumps(d)
    t2 = time.perf_counter()
    conv_us = (t1 - t0) / n * 1e6
    dump_us = (t2 - t1) / n * 1e6
    print(
        f"{label:8s} n={n}: to_dicom_json {conv_us:8.1f} us/item | "
        f"json.dumps {dump_us:6.1f} us/item | total {(conv_us + dump_us):8.1f} us/item "
        f"| items/s {1e6 / (conv_us + dump_us):8.0f}"
    )


if __name__ == "__main__":
    print(f"python {sys.version}")
    bench("qido", make_qido_ds(), 5000)
    bench("header", make_header_ds(), 2000)
