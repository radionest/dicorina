"""Write the synthetic studies from study_plan as tiny CT instances (pydicom).

Runs inside the PACS golden-image build, where pydicom is installed. Not imported
by host unit tests. Output is one .dcm per instance under --out."""

import argparse
import glob
import os

import numpy as np
import study_plan
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

ROWS = COLS = 32


def _write_instance(out_dir, study, sop_uid):
    meta = Dataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = generate_uid()

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.PatientName = study["PatientName"]
    ds.PatientID = study["PatientID"]
    ds.StudyInstanceUID = study["StudyInstanceUID"]
    ds.SeriesInstanceUID = study["SeriesInstanceUID"]
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.Modality = "CT"
    ds.Rows = ROWS
    ds.Columns = COLS
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.zeros((ROWS, COLS), dtype=np.uint16).tobytes()
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.save_as(os.path.join(out_dir, sop_uid + ".dcm"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan", choices=("e2e", "bench"), default="e2e")
    ap.add_argument("--studies", type=int, default=3)
    ap.add_argument("--instances", type=int, default=1000)
    ap.add_argument("--big-instances", type=int, default=1000)
    ap.add_argument("--find-studies", type=int, default=15)
    ap.add_argument("--find-instances", type=int, default=2)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for stale in glob.glob(os.path.join(args.out, "*.dcm")):
        os.remove(
            stale
        )  # drop stale instances so a smaller --studies/--instances can't leave extras
    if args.plan == "bench":
        bench = study_plan.build_bench_plan(
            args.big_instances, args.find_studies, args.find_instances
        )
        plan = bench["big"] + bench["multi"]
    else:
        plan = study_plan.build_study_plan(args.studies, args.instances)
    n = 0
    for study in plan:
        for sop in study["SOPInstanceUIDs"]:
            _write_instance(args.out, study, sop)
            n += 1
    print(f"wrote {n} instances for {len(plan)} studies to {args.out}")


if __name__ == "__main__":
    main()
