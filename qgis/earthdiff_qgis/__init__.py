"""earthdiff for QGIS: what changed here? Change detection from free Sentinel-2 imagery."""


def classFactory(iface):  # noqa: N802 (QGIS entry point)
    from .plugin import EarthdiffPlugin
    return EarthdiffPlugin(iface)
