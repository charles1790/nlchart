"""Basemap source definitions for the chart types nlchart currently understands.

Each source is a live ArcGIS tile-cache MapServer, loaded in QGIS via the
"arcgismapserver" raster provider (not a hand-rolled XYZ template) because
these services use non-standard level-of-detail pyramids that only the
arcgismapserver provider resolves correctly.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BasemapSpec:
    key: str
    title: str
    uri: str
    attribution: str
    # Placeholder default view used until phase 2 adds real NL area
    # understanding: a center point plus a cartographic scale denominator
    # (e.g. 500_000 means 1:500,000). A fixed WGS84 bbox doesn't work here --
    # NOAA/FAA raster tile caches only render within a service-defined scale
    # band (e.g. VFR_Sectional is only visible between roughly 1:144k and
    # 1:2.3M, matching the real-world scale of a paper sectional), so the
    # default has to be expressed as a scale, not an extent.
    default_center_wgs84: tuple  # (lon, lat)
    default_scale: float
    provider: str = "arcgismapserver"


_DEFAULT_CENTER = (-122.3321, 47.6062)  # Seattle, WA -- coastal + charted + imaged everywhere

BASEMAPS = {
    "nautical": BasemapSpec(
        key="nautical",
        title="Nautical Chart",
        uri="https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/NOAACharts/MapServer",
        attribution="NOAA Office of Coast Survey",
        default_center_wgs84=_DEFAULT_CENTER,
        default_scale=80_000,
    ),
    "sectional": BasemapSpec(
        key="sectional",
        title="VFR Sectional Chart",
        uri="https://tiles.arcgis.com/tiles/ssFJjBXIUyZDrSYZ/arcgis/rest/services/VFR_Sectional/MapServer",
        attribution="FAA Aeronautical Information Services",
        default_center_wgs84=_DEFAULT_CENTER,
        default_scale=500_000,  # the actual published scale of a VFR sectional
    ),
    "satellite": BasemapSpec(
        key="satellite",
        title="Satellite Image",
        uri="https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer",
        attribution="Esri, Maxar, Earthstar Geographics",
        default_center_wgs84=_DEFAULT_CENTER,
        default_scale=500_000,
    ),
    "topo": BasemapSpec(
        key="topo",
        title="USGS Topo",
        uri="https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer",
        attribution="USGS The National Map",
        # Mt. Rainier -- unlike the other three types, this one is for
        # land/mountain rescue, so a mountainous default reads better than
        # the coastal Seattle default the marine/aeronautical/imagery types
        # share.
        default_center_wgs84=(-121.7603, 46.8523),
        default_scale=24_000,  # the actual published scale of a USGS 7.5' topo quad
    ),
}
