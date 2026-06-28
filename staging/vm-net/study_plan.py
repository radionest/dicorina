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
