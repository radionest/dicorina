from dicorina.http_face.ohif import (
    DATASOURCES_SENTINEL,
    inject_datasources,
    render_datasources_js,
)


def test_render_points_at_dicom_web() -> None:
    js = render_datasources_js(friendly_name="dicorina", base_path="")
    assert '"qidoRoot": "/dicom-web"' in js
    assert '"wadoRoot": "/dicom-web"' in js


def test_inject_replaces_sentinel() -> None:
    text = f"window.config = {{ dataSources: {DATASOURCES_SENTINEL} }};"
    out = inject_datasources(text, "[]")
    assert out is not None
    assert DATASOURCES_SENTINEL not in out


def test_inject_returns_none_without_sentinel() -> None:
    assert inject_datasources("no sentinel here", "[]") is None
