"""Render the OHIF app-config.js dataSources block (datasources → /dicom-web)."""

from __future__ import annotations

import json

DATASOURCES_SENTINEL = "__DICORINA_DATASOURCES__"


def render_datasources_js(
    *, friendly_name: str, base_path: str, external_root: str | None = None
) -> str:
    base = base_path.rstrip("/")
    root = f"{base}{external_root}" if external_root else f"{base}/dicom-web"
    datasources = [
        {
            "namespace": "@ohif/extension-default.dataSourcesModule.dicomweb",
            "sourceName": "dicomweb",
            "configuration": {
                "friendlyName": friendly_name,
                "name": "dicorina",
                "wadoUriRoot": root,
                "qidoRoot": root,
                "wadoRoot": root,
                "qidoSupportsIncludeField": False,
                "imageRendering": "wadors",
                "thumbnailRendering": "wadors",
                "supportsFuzzyMatching": False,
                "supportsWildcard": True,
            },
        }
    ]
    return json.dumps(datasources, indent=2)


def inject_datasources(app_config_text: str, datasources_js: str) -> str | None:
    if DATASOURCES_SENTINEL not in app_config_text:
        return None
    return app_config_text.replace(DATASOURCES_SENTINEL, datasources_js)
