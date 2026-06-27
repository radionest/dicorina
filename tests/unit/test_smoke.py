import dicorina


def test_package_imports() -> None:
    assert dicorina.__version__ == "0.1.0"


def test_dimsechord_is_available() -> None:
    from dimsechord import AssociationPool, DicomClient, PullEngine, StorageSCP

    assert AssociationPool(["X"], 1).total_capacity == 1
    assert DicomClient and PullEngine and StorageSCP
