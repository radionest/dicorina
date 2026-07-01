import dicorina


def test_package_imports() -> None:
    assert dicorina.__version__ == "0.1.0"


def test_dimsechord_is_available() -> None:
    from dimsechord import AssociationPool, DicomClient, PullEngine, StorageSCP

    assert AssociationPool(["X"], 1).total_capacity == 1
    assert DicomClient and PullEngine and StorageSCP


def test_create_app_silences_pydicom_value_warnings(tmp_path) -> None:
    import pydicom.config

    from dicorina.app import create_app
    from dicorina.config import DicorinaConfig

    pydicom.config.settings.reading_validation_mode = pydicom.config.WARN
    cfg = DicorinaConfig.model_validate(
        {"pacs": {"host": "127.0.0.1"}, "scp": {}, "cache": {"dir": str(tmp_path)}}
    )
    create_app(cfg)
    assert pydicom.config.settings.reading_validation_mode == pydicom.config.IGNORE
