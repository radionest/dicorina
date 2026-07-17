from importlib.metadata import version

import dicorina


def test_package_imports() -> None:
    assert dicorina.__version__ == version("dicorina")


def test_dimsechord_is_available() -> None:
    from dimsechord import AssociationPool, DicomClient, PullEngine, StorageSCP

    assert AssociationPool(["X"], 1).total_capacity == 1
    assert DicomClient and PullEngine and StorageSCP


def test_configure_pydicom_ignores_value_warnings() -> None:
    import pydicom.config

    from dicorina.app import _configure_pydicom

    original = pydicom.config.settings.reading_validation_mode
    try:
        pydicom.config.settings.reading_validation_mode = pydicom.config.WARN
        _configure_pydicom()
        assert pydicom.config.settings.reading_validation_mode == pydicom.config.IGNORE
    finally:
        pydicom.config.settings.reading_validation_mode = original
