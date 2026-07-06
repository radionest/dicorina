"""Pure planning for synthetic studies — no pydicom, host-unit-testable."""

CYRILLIC_NAME = "Иванов^Пётр"
ROOT = "1.2.826.0.1.3680043.8.498"


def build_study_plan(num_studies=3, instances_per_study=1000):
    if num_studies < 1 or instances_per_study < 1:
        raise ValueError(
            f"num_studies and instances_per_study must be >= 1 "
            f"(got {num_studies}, {instances_per_study})"
        )
    studies = []
    for s in range(num_studies):
        study_uid = f"{ROOT}.{s + 1}"
        series_uid = f"{study_uid}.1"
        name = CYRILLIC_NAME if s == 0 else f"Patient^{s + 1}"
        sops = [f"{series_uid}.{i + 1}" for i in range(instances_per_study)]
        studies.append(
            {
                "StudyInstanceUID": study_uid,
                "SeriesInstanceUID": series_uid,
                "PatientName": name,
                "PatientID": f"VMNET{s + 1:03d}",
                "SOPInstanceUIDs": sops,
            }
        )
    return studies


def build_bench_plan(big_instances=1000, find_studies=15, find_instances=2):
    """Bench-only data: 2 big studies (move/WADO) + one patient with many small
    studies (C-FIND). UID namespace ROOT.10x / ROOT.20x is disjoint from the
    e2e plan (ROOT.1..6) so bench imports never collide with golden data."""
    if big_instances < 1 or find_studies < 1 or find_instances < 1:
        raise ValueError(
            f"big_instances, find_studies and find_instances must be >= 1 "
            f"(got {big_instances}, {find_studies}, {find_instances})"
        )

    def study(study_uid, name, pid, n):
        series_uid = f"{study_uid}.1"
        return {
            "StudyInstanceUID": study_uid,
            "SeriesInstanceUID": series_uid,
            "PatientName": name,
            "PatientID": pid,
            "SOPInstanceUIDs": [f"{series_uid}.{i + 1}" for i in range(n)],
        }

    return {
        "big": [
            study(f"{ROOT}.{100 + s}", f"Bench^Big{s}", f"BENCH{s:03d}", big_instances)
            for s in (1, 2)
        ],
        "multi": [
            study(f"{ROOT}.{200 + k}", "Bench^Multi", "BENCH100", find_instances)
            for k in range(1, find_studies + 1)
        ],
    }
