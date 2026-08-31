"""Headless PyQGIS rendering: ChartSpec -> printable PDF."""

import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import List

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsProject,
    QgsRasterLayer,
    QgsDataSourceUri,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
    QgsPrintLayout,
    QgsLayoutItemMap,
    QgsLayoutItemLabel,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsUnitTypes,
    QgsLayoutExporter,
    QgsTextFormat,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsMarkerSymbol,
    QgsSvgMarkerSymbolLayer,
    QgsLineSymbol,
    QgsFillSymbol,
    QgsSimpleFillSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsPalLayerSettings,
    QgsVectorLayerSimpleLabeling,
    QgsLayoutItemPicture,
    QgsLayoutItemMapGrid,
    QgsDistanceArea,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QImage

from .basemaps import BASEMAPS, BasemapSpec
from .geo_math import (
    circle_points,
    meters_to_unit,
    route_distance_m,
    route_geometry_points,
    sq_meters_to_unit,
)
from .spec import ChartSpec, LineSpec, PointSetSpec, PolygonSpec, RangeRingSpec

_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

_QGIS_APP = None

PAGE_WIDTH_MM = 432.0  # 17in, ANSI D / tabloid-landscape family
PAGE_HEIGHT_MM = 279.0  # 11in
MARGIN_MM = 10.0
TITLE_HEIGHT_MM = 14.0
FOOTER_HEIGHT_MM = 8.0
# A dedicated band below the map frame for the scale bar (measured: a
# default-sized "Single Box" QgsLayoutItemScaleBar is ~17.5mm tall).
# Without this the map frame's bottom edge sits flush against the footer
# (zero gap), so the scale bar -- previously tucked 12mm inside that edge --
# collided with the graticule's zebra frame border and inside-frame
# longitude labels along the bottom.
SCALEBAR_ZONE_MM = 22.0
_SCALEBAR_TOP_GAP_MM = 2.0
# Per chart type, not a single global constant: the blank-tile export bug
# (see _SCALE_BOUNDS below) turned out to be specific to how close a chart
# type's padded minimum scale sits to its tile service's real max-detail
# edge, not a universal "high DPI near any zoom" problem. Verified directly
# (rendering each type at its own tightest _SCALE_BOUNDS floor and checking
# for blank output, the same way the original bug was diagnosed):
# - nautical clean at 300 DPI even at its floor (1:20,000) -- NOAA's padded
#   floor has huge headroom past the service's real ~1:4,514 edge.
# - satellite clean at 300 DPI even at a far tighter zoom than any bound
#   allows (no _SCALE_BOUNDS entry -- Esri World Imagery has no scale
#   limit at all).
# - sectional blanks out above ~170-180 DPI at its floor (1:175,000) --
#   FAA's own service max-detail edge (~1:144,448) is close enough to that
#   padded floor that it stays fragile regardless. Left at the original
#   150 DPI, well under the observed ~170/180 cliff.
# - topo clean at 300 DPI at its floor (1:20,000) -- USGS National Map's
#   USGSTopo service uses a full standard 24-level global Web Mercator
#   pyramid (real detail down to ~1:70), not a narrow published-chart scale
#   band like FAA's, so like nautical it has generous headroom. Bisected
#   the same way: blank below ~1:19,000 at 300 DPI (an export-path artifact,
#   not a real tile gap -- native tiles exist well past that), clean at
#   1:20,000 and up, including well past the padded ceiling.
_EXPORT_DPI_BY_TYPE = {
    "nautical": 300.0,
    "sectional": 150.0,
    "satellite": 300.0,
    "topo": 300.0,
}

# Cartographic scale denominator bounds these tile services actually render
# within. The service-advertised min/maxScale (nautical 4,513.99-295,828,763.80;
# sectional 144,447.64-2,311,162.22, from their REST metadata) is only the
# start of the story -- empirically (bisecting against real renders, not
# guessed), QGIS's print-layout export for these cached ArcGIS tile services
# blanks out for a scale within roughly 5-8% of the max-detail (lowest
# denominator) edge, even though a direct in-memory render at the same
# extent/resolution works fine. The lower bounds here are padded well past
# that empirically-observed failure zone, not just past the service's
# theoretical limit. No entry for "satellite" -- Esri World Imagery hit none
# of this in testing. "topo" got the same bisection treatment as nautical/
# sectional (see _EXPORT_DPI_BY_TYPE) and, like nautical, only needed a
# floor -- no blank-render issue found anywhere near its zoomed-out end.
_SCALE_BOUNDS = {
    "nautical": (20_000, 280_000_000),
    "sectional": (175_000, 2_200_000),
    "topo": (20_000, 280_000_000),
}

_POINT_COLORS = {
    "red": "#e6194b",
    "green": "#3cb44b",
    "blue": "#4363d8",
    "yellow": "#ffe119",
    "orange": "#f58231",
    "black": "#000000",
    "white": "#ffffff",
    "purple": "#911eb4",
}

# Paths relative to QGIS's own svg/ resource dir (QgsApplication.pkgDataPath()),
# not hard-coded absolute paths -- portable across QGIS installs. These are
# QGIS-bundled icons using its "param(fill)" SVG convention, so their color
# is recolorable via QgsSvgMarkerSymbolLayer.setFillColor() rather than being
# baked into the file. "dot" has no SVG -- it's the plain circle marker.
_POINT_ICON_SVGS = {
    "boat": "gpsicons/boat.svg",
    "helicopter": "transport/transport_helicopter.svg",
    "plane": "gpsicons/plane.svg",
    "car": "gpsicons/car.svg",
    "flag": "gpsicons/flag.svg",
    "house": "gpsicons/house.svg",
    "anchor": "gpsicons/anchor.svg",
}

# Keep some surrounding context even when all points are clustered tightly
# together, and pad the bounding box so points don't sit flush against the
# page edge.
_MIN_POINT_EXTENT_M = 2_000.0
_POINT_PADDING_FACTOR = 1.4

# Areas (radius/bounds/region-extent) are already an intentional frame
# request, so they get a smaller floor and gentler padding than points --
# just enough that a boundary landmark doesn't sit clipped exactly on the
# page edge.
_MIN_AREA_EXTENT_M = 500.0
_AREA_PADDING_FACTOR = 1.15

# All four chart types render in a fixed, never-rotated CRS (map_item's
# rotation is never set), so a static up-pointing arrow is always correct --
# no need to track map rotation.
_NORTH_ARROW_SVG = "arrows/NorthArrow_04.svg"
_NORTH_ARROW_WIDTH_MM = 8.0
_NORTH_ARROW_HEIGHT_MM = 12.0
_NORTH_ARROW_INSET_MM = 4.0

# "Nice" graticule intervals in degrees, smallest first -- picked to keep
# the grid to roughly 4-8 lines across the map frame's longer WGS84 span,
# same spirit as _SCALE_BOUNDS' verified-round-numbers approach rather than
# relying on any QGIS auto-interval behavior (none found on this API).
_GRATICULE_INTERVALS_DEG = [
    0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10,
]
_GRATICULE_TARGET_LINES = 6


class ChartRenderError(RuntimeError):
    pass


def _ensure_qgis_app() -> QgsApplication:
    global _QGIS_APP
    if _QGIS_APP is None:
        prefix = os.environ.get("QGIS_PREFIX_PATH", "/usr")
        QgsApplication.setPrefixPath(prefix, True)
        _QGIS_APP = QgsApplication([], False)
        _QGIS_APP.initQgis()
    return _QGIS_APP


def _add_basemap_layer(project: QgsProject, chart_type: str) -> QgsRasterLayer:
    if chart_type not in BASEMAPS:
        known = ", ".join(sorted(BASEMAPS))
        raise ChartRenderError(f"Unknown chart type {chart_type!r}; known types: {known}")

    spec = BASEMAPS[chart_type]
    source_uri = QgsDataSourceUri()
    source_uri.setParam("url", spec.uri)
    layer = QgsRasterLayer(source_uri.uri(False), spec.title, spec.provider)
    if not layer.isValid():
        raise ChartRenderError(
            f"Basemap layer for {chart_type!r} failed to load from {spec.uri}"
        )
    project.addMapLayer(layer)
    return layer


def _build_point_symbol(point_set: PointSetSpec) -> QgsMarkerSymbol:
    color = QColor(_POINT_COLORS[point_set.color])
    svg_relpath = _POINT_ICON_SVGS.get(point_set.icon)

    if svg_relpath is None:  # "dot" -- plain circle, no icon
        return QgsMarkerSymbol.createSimple(
            {
                "name": "circle",
                "color": _POINT_COLORS[point_set.color],
                "outline_color": "black",
                "outline_width": "0.4",
                "size": "4",
            }
        )

    svg_path = os.path.join(_QGIS_APP.pkgDataPath(), "svg", svg_relpath)
    svg_layer = QgsSvgMarkerSymbolLayer(svg_path)
    svg_layer.setFillColor(color)
    svg_layer.setStrokeColor(QColor("black"))
    svg_layer.setStrokeWidth(0.3)
    svg_layer.setSize(8)
    symbol = QgsMarkerSymbol()
    symbol.changeSymbolLayer(0, svg_layer)
    return symbol


def _build_points_layer(point_set: PointSetSpec, index: int) -> QgsVectorLayer:
    layer = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=label:string(200)", f"points_{index}", "memory"
    )
    provider = layer.dataProvider()
    features = []
    for point in point_set.points:
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point.lon, point.lat)))
        feature.setAttributes([point.label])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()

    layer.setRenderer(QgsSingleSymbolRenderer(_build_point_symbol(point_set)))

    label_settings = QgsPalLayerSettings()
    label_settings.fieldName = "label"
    text_format = QgsTextFormat()
    text_format.setSize(9)
    buffer_settings = text_format.buffer()
    buffer_settings.setEnabled(True)
    buffer_settings.setSize(1)
    text_format.setBuffer(buffer_settings)
    label_settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
    layer.setLabelsEnabled(True)

    return layer


def _add_point_layers(project: QgsProject, chart_spec: ChartSpec) -> List[QgsVectorLayer]:
    layers = [_build_points_layer(ps, i) for i, ps in enumerate(chart_spec.point_sets)]
    for point_layer in layers:
        project.addMapLayer(point_layer)
    return layers


def _build_line_symbol(line: LineSpec) -> QgsLineSymbol:
    return QgsLineSymbol.createSimple(
        {
            "line_color": _POINT_COLORS[line.color],
            "line_width": "0.6",
        }
    )


def _build_line_layer(line: LineSpec, index: int) -> QgsVectorLayer:
    layer = QgsVectorLayer(
        "LineString?crs=EPSG:4326&field=label:string(300)", f"line_{index}", "memory"
    )
    provider = layer.dataProvider()

    geometry_points = route_geometry_points(line.waypoints_wgs84, line.line_type)
    qgs_points = [QgsPointXY(lon, lat) for lat, lon in geometry_points]

    distance_m = route_distance_m(line.waypoints_wgs84, line.line_type)
    if line.show_distance:
        distance = meters_to_unit(distance_m, line.distance_unit)
        label_text = f"{line.label} -- {distance:.1f} {line.distance_unit}"
    else:
        label_text = line.label

    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromPolylineXY(qgs_points))
    feature.setAttributes([label_text])
    provider.addFeatures([feature])
    layer.updateExtents()

    layer.setRenderer(QgsSingleSymbolRenderer(_build_line_symbol(line)))

    label_settings = QgsPalLayerSettings()
    label_settings.fieldName = "label"
    label_settings.placement = QgsPalLayerSettings.Placement.Curved
    text_format = QgsTextFormat()
    text_format.setSize(9)
    buffer_settings = text_format.buffer()
    buffer_settings.setEnabled(True)
    buffer_settings.setSize(1)
    text_format.setBuffer(buffer_settings)
    label_settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
    layer.setLabelsEnabled(True)

    return layer


def _add_line_layers(project: QgsProject, chart_spec: ChartSpec) -> List[QgsVectorLayer]:
    layers = [_build_line_layer(line, i) for i, line in enumerate(chart_spec.lines)]
    for line_layer in layers:
        project.addMapLayer(line_layer)
    return layers


def _build_polygon_symbol(polygon: PolygonSpec) -> QgsFillSymbol:
    color = QColor(_POINT_COLORS[polygon.color])
    fill_layer = QgsSimpleFillSymbolLayer()
    fill_layer.setColor(color)
    # The outline follows the requested color too, not a hardcoded black --
    # every other overlay type (points, lines, range rings) already uses
    # the requested color directly with no such override. This mattered
    # most for "unfilled" (outline-only, Qt.NoBrush below): with a fixed
    # black stroke and no fill, the requested color was never visible on
    # the chart at all, regardless of what was asked for.
    fill_layer.setStrokeColor(color)
    fill_layer.setStrokeWidth(0.6)

    # "shaded" uses a real Qt.BrushStyle enum value, not createSimple()'s
    # string-key shortcut -- verified some of those string keys (e.g.
    # 'b_diagonal', 'dense6'/'dense7') silently mis-apply color, while the
    # enum-based FDiagPattern renders reliably and reads as a lighter,
    # basemap-preserving fill than a denser cross-hatch.
    if polygon.fill_style == "filled":
        fill_layer.setBrushStyle(Qt.SolidPattern)
    elif polygon.fill_style == "shaded":
        fill_layer.setBrushStyle(Qt.FDiagPattern)
    else:  # "unfilled" -- outline only
        fill_layer.setBrushStyle(Qt.NoBrush)

    symbol = QgsFillSymbol()
    symbol.changeSymbolLayer(0, fill_layer)
    return symbol


def _measure_polygon_geodesic(geometry: QgsGeometry):
    """Real-world (area_m2, perimeter_m) for a WGS84 polygon geometry, via
    QGIS's own ellipsoidal distance/area calculator -- verified against a
    manually-computed box before being trusted (see plan notes), so this
    doesn't need a hand-rolled spherical-polygon-area formula the way lines'
    distance math does."""
    distance_area = QgsDistanceArea()
    distance_area.setEllipsoid("WGS84")
    distance_area.setSourceCrs(_WGS84, QgsProject.instance().transformContext())
    return distance_area.measureArea(geometry), distance_area.measurePerimeter(geometry)


def _build_polygon_layer(polygon: PolygonSpec, index: int) -> QgsVectorLayer:
    layer = QgsVectorLayer(
        "Polygon?crs=EPSG:4326&field=label:string(200)", f"polygon_{index}", "memory"
    )
    provider = layer.dataProvider()

    qgs_points = [QgsPointXY(lon, lat) for lat, lon in polygon.vertices_wgs84]
    geometry = QgsGeometry.fromPolygonXY([qgs_points])

    measurements = []
    if polygon.show_area or polygon.show_perimeter:
        area_m2, perimeter_m = _measure_polygon_geodesic(geometry)
        if polygon.show_area:
            area = sq_meters_to_unit(area_m2, polygon.area_unit)
            measurements.append(f"{area:.1f} {polygon.area_unit}")
        if polygon.show_perimeter:
            perimeter = meters_to_unit(perimeter_m, polygon.perimeter_unit)
            measurements.append(f"{perimeter:.1f} {polygon.perimeter_unit}")
    label_text = f"{polygon.label} -- {', '.join(measurements)}" if measurements else polygon.label

    feature = QgsFeature()
    feature.setGeometry(geometry)
    feature.setAttributes([label_text])
    provider.addFeatures([feature])
    layer.updateExtents()

    layer.setRenderer(QgsSingleSymbolRenderer(_build_polygon_symbol(polygon)))

    label_settings = QgsPalLayerSettings()
    label_settings.fieldName = "label"
    text_format = QgsTextFormat()
    text_format.setSize(9)
    buffer_settings = text_format.buffer()
    buffer_settings.setEnabled(True)
    buffer_settings.setSize(1)
    text_format.setBuffer(buffer_settings)
    label_settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
    layer.setLabelsEnabled(True)

    return layer


def _add_polygon_layers(project: QgsProject, chart_spec: ChartSpec) -> List[QgsVectorLayer]:
    layers = [_build_polygon_layer(polygon, i) for i, polygon in enumerate(chart_spec.polygons)]
    for polygon_layer in layers:
        project.addMapLayer(polygon_layer)
    return layers


def _build_range_ring_symbol(ring: RangeRingSpec) -> QgsLineSymbol:
    # Dashed, distinct from route lines (solid) -- a ring is a distance
    # marker/planning aid, not a navigable path.
    return QgsLineSymbol.createSimple(
        {
            "line_color": _POINT_COLORS[ring.color],
            "line_width": "0.5",
            "line_style": "dash",
        }
    )


def _build_range_ring_layer(ring: RangeRingSpec, index: int) -> QgsVectorLayer:
    layer = QgsVectorLayer(
        "LineString?crs=EPSG:4326&field=label:string(200)", f"range_rings_{index}", "memory"
    )
    provider = layer.dataProvider()

    lat0, lon0 = ring.center_wgs84
    features = []
    for distance_m in ring.ring_distances_m:
        ring_pts = circle_points(lat0, lon0, distance_m)
        qgs_points = [QgsPointXY(lon, lat) for lat, lon in ring_pts]
        distance_label = meters_to_unit(distance_m, ring.distance_unit)

        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPolylineXY(qgs_points))
        feature.setAttributes([f"{ring.label} -- {distance_label:.1f} {ring.distance_unit}"])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()

    layer.setRenderer(QgsSingleSymbolRenderer(_build_range_ring_symbol(ring)))

    label_settings = QgsPalLayerSettings()
    label_settings.fieldName = "label"
    label_settings.placement = QgsPalLayerSettings.Placement.Curved
    text_format = QgsTextFormat()
    text_format.setSize(8)
    buffer_settings = text_format.buffer()
    buffer_settings.setEnabled(True)
    buffer_settings.setSize(1)
    text_format.setBuffer(buffer_settings)
    label_settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
    layer.setLabelsEnabled(True)

    return layer


def _add_range_ring_layers(project: QgsProject, chart_spec: ChartSpec) -> List[QgsVectorLayer]:
    layers = [_build_range_ring_layer(ring, i) for i, ring in enumerate(chart_spec.range_rings)]
    for ring_layer in layers:
        project.addMapLayer(ring_layer)
    return layers


def _bbox_center_and_scale(
    lonlat_points,
    layer: QgsRasterLayer,
    frame_width_mm: float,
    frame_height_mm: float,
    padding_factor: float,
    min_extent_m: float,
):
    """Return (projected_cx, projected_cy, scale) that fits the bounding box
    of the given (lon, lat) points inside the map frame. Shared by points
    auto-fit and every area mode (radius/bounds/region-extent all reduce to
    "here are some WGS84 points, frame around them").

    Deliberately does the whole computation in the layer's projected CRS
    (Web Mercator), not in degrees-converted-to-approximate-meters: Mercator
    stretches north-south distance by 1/cos(lat), which at high latitudes
    (e.g. ~2x at 60 deg N) is nowhere close to 1 -- estimating spans in true
    ground meters and then treating them as projected-plane meters clips
    points that are actually inside the intended coverage area.
    """
    projected = [_project_point(lon, lat, layer) for lon, lat in lonlat_points]
    xs = [p[0] for p in projected]
    ys = [p[1] for p in projected]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2

    width_span = max(x_max - x_min, min_extent_m) * padding_factor
    height_span = max(y_max - y_min, min_extent_m) * padding_factor

    frame_width_m = frame_width_mm / 1000.0
    frame_height_m = frame_height_mm / 1000.0
    scale = max(width_span / frame_width_m, height_span / frame_height_m)
    return cx, cy, scale


# The blank-tile export bug documented above (_SCALE_BOUNDS) turned out to
# be broader than "close to a padded floor": rendering the exact same
# scale/DPI at different real-world locations found the actual failure
# threshold varies by *where* the chart is, not just how zoomed in it is --
# e.g. sectional at 1:226,208 renders fine over Anchorage but blank over
# Pittsburgh, and Pittsburgh only clears at roughly 1:350,000+. A single
# static per-type floor can't account for this since it depends on
# region-specific tile/service behavior, not a documented scale limit. So
# instead of trying to widen _SCALE_BOUNDS further (which would just be
# guessing at another number that might still fail somewhere else),
# render_chart detects an actual blank result and steps the scale back
# until it clears, at the exact location being rendered.
#
# Verified this detection method itself: a *direct in-memory* render
# (QgsMapRendererParallelJob) does NOT reproduce the bug at all (matching
# the original diagnosis that it's specific to the print-layout export
# path) -- but a full QgsLayoutExporter image export at a small page size
# reproduces the identical blank/clean pattern as a full production-size
# page at the same scale/DPI, so a small export is a fast, faithful proxy
# for "will the real export be blank here."
_BLANK_CHECK_PAGE_MM = (60.0, 45.0)
_BLANK_CHECK_SAMPLE_STRIDE = 37  # prime stride -- avoids aliasing with any regular pixel pattern
_BLANK_CHECK_VARIANCE_THRESHOLD = 5
_BLANK_SCALE_STEP_FACTOR = 1.4
_BLANK_SCALE_MAX_RETRIES = 8


def _is_blank_render(layer: QgsRasterLayer, cx: float, cy: float, scale: float, dpi: float) -> bool:
    width_mm, height_mm = _BLANK_CHECK_PAGE_MM
    extent = _rect_for_projected_center_and_scale(cx, cy, scale, width_mm, height_mm)

    layout = QgsPrintLayout(QgsProject.instance())
    layout.initializeDefaults()
    page = layout.pageCollection().pages()[0]
    page.setPageSize(QgsLayoutSize(width_mm, height_mm, QgsUnitTypes.LayoutMillimeters))

    map_item = QgsLayoutItemMap(layout)
    layout.addLayoutItem(map_item)
    map_item.attemptMove(QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters))
    map_item.attemptResize(QgsLayoutSize(width_mm, height_mm, QgsUnitTypes.LayoutMillimeters))
    map_item.setLayers([layer])
    map_item.setCrs(layer.crs())
    map_item.zoomToExtent(extent)

    fd, out_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    os.remove(out_path)  # exportToImage errors on an existing file (GDAL PNG driver quirk)
    try:
        exporter = QgsLayoutExporter(layout)
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = dpi
        exporter.exportToImage(out_path, settings)
        image = QImage(out_path)
        buf = image.bits().asstring(image.sizeInBytes())
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)

    sample = buf[::_BLANK_CHECK_SAMPLE_STRIDE]
    return (max(sample) - min(sample)) < _BLANK_CHECK_VARIANCE_THRESHOLD


def _avoid_blank_render(chart_type: str, layer: QgsRasterLayer, cx: float, cy: float, scale: float) -> float:
    dpi = _EXPORT_DPI_BY_TYPE.get(chart_type, 300.0)
    _, ceiling = _SCALE_BOUNDS.get(chart_type, (None, None))

    for _ in range(_BLANK_SCALE_MAX_RETRIES):
        if not _is_blank_render(layer, cx, cy, scale, dpi):
            return scale
        if ceiling is not None and scale >= ceiling:
            break
        scale = min(scale * _BLANK_SCALE_STEP_FACTOR, ceiling) if ceiling is not None else scale * _BLANK_SCALE_STEP_FACTOR

    print(
        f"warning: {chart_type} chart renders blank at this location for every scale tried, "
        f"up to 1:{scale:,.0f} -- showing the widest scale tried anyway",
        file=sys.stderr,
    )
    return scale


def _clamp_scale(chart_type: str, requested_scale: float):
    """Returns (scale_to_use, warning_message_or_None)."""
    bounds = _SCALE_BOUNDS.get(chart_type)
    if bounds is None:
        return requested_scale, None
    lo, hi = bounds
    if lo <= requested_scale <= hi:
        return requested_scale, None
    clamped = min(max(requested_scale, lo), hi)
    message = (
        f"requested area implies a scale of 1:{requested_scale:,.0f} for a {chart_type} "
        f"chart, which only renders between 1:{lo:,.0f} and 1:{hi:,.0f} -- "
        f"showing 1:{clamped:,.0f} instead"
    )
    return clamped, message


def _project_point(lon: float, lat: float, layer: QgsRasterLayer):
    """Transform a WGS84 (lon, lat) into layer's CRS; returns (x, y)."""
    point = QgsRectangle(lon, lat, lon, lat)
    if layer.crs() != _WGS84:
        transform = QgsCoordinateTransform(_WGS84, layer.crs(), QgsProject.instance())
        point = transform.transformBoundingBox(point)
    return point.xMinimum(), point.yMinimum()


def _rect_for_projected_center_and_scale(
    cx: float, cy: float, scale: float, frame_width_mm: float, frame_height_mm: float
) -> QgsRectangle:
    """Build an extent centered on an already-projected (cx, cy) sized so the
    map frame renders at the given cartographic scale (ground_dist / paper_dist)."""
    extent_width = scale * (frame_width_mm / 1000.0)
    extent_height = scale * (frame_height_mm / 1000.0)
    return QgsRectangle(
        cx - extent_width / 2,
        cy - extent_height / 2,
        cx + extent_width / 2,
        cy + extent_height / 2,
    )


def _resolve_extent(
    chart_spec: ChartSpec, spec: BasemapSpec, layer: QgsRasterLayer, frame_width_mm: float, frame_height_mm: float
) -> QgsRectangle:
    if chart_spec.area is not None:
        south, west, north, east = chart_spec.area.bounds_wgs84
        cx, cy, requested_scale = _bbox_center_and_scale(
            [(west, south), (east, north)],
            layer,
            frame_width_mm,
            frame_height_mm,
            _AREA_PADDING_FACTOR,
            _MIN_AREA_EXTENT_M,
        )
    elif chart_spec.point_sets or chart_spec.lines or chart_spec.polygons or chart_spec.range_rings:
        # No explicit area, but points/lines/polygons/range_rings were given
        # -- auto-fit to them rather than falling back to the placeholder
        # region, which would be useless (or actively misleading) if
        # they're elsewhere. For a great-circle line this uses the full
        # densified arc, not just its endpoints, so a bowed arc doesn't get
        # clipped by the frame.
        lonlat_points = [(p.lon, p.lat) for ps in chart_spec.point_sets for p in ps.points]
        for line in chart_spec.lines:
            lonlat_points += [
                (lon, lat) for lat, lon in route_geometry_points(line.waypoints_wgs84, line.line_type)
            ]
        for polygon in chart_spec.polygons:
            lonlat_points += [(lon, lat) for lat, lon in polygon.vertices_wgs84]
        for ring in chart_spec.range_rings:
            outer_radius_m = max(ring.ring_distances_m)
            lat0, lon0 = ring.center_wgs84
            lonlat_points += [(lon, lat) for lat, lon in circle_points(lat0, lon0, outer_radius_m)]
        cx, cy, requested_scale = _bbox_center_and_scale(
            lonlat_points, layer, frame_width_mm, frame_height_mm, _POINT_PADDING_FACTOR, _MIN_POINT_EXTENT_M
        )
    else:
        # Phase-1 fallback: fixed placeholder region + the type's default scale.
        lon, lat = spec.default_center_wgs84
        cx, cy = _project_point(lon, lat, layer)
        requested_scale = spec.default_scale

    scale, warning = _clamp_scale(chart_spec.chart_type, requested_scale)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    scale = _avoid_blank_render(chart_spec.chart_type, layer, cx, cy, scale)
    return _rect_for_projected_center_and_scale(cx, cy, scale, frame_width_mm, frame_height_mm)


def _build_north_arrow(layout: QgsPrintLayout, map_right: float, map_top: float) -> QgsLayoutItemPicture:
    svg_path = os.path.join(_QGIS_APP.pkgDataPath(), "svg", _NORTH_ARROW_SVG)
    picture = QgsLayoutItemPicture(layout)
    picture.setPicturePath(svg_path)
    layout.addLayoutItem(picture)
    picture.attemptResize(QgsLayoutSize(_NORTH_ARROW_WIDTH_MM, _NORTH_ARROW_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))
    picture.attemptMove(
        QgsLayoutPoint(
            map_right - _NORTH_ARROW_WIDTH_MM - _NORTH_ARROW_INSET_MM,
            map_top + _NORTH_ARROW_INSET_MM,
            QgsUnitTypes.LayoutMillimeters,
        )
    )
    return picture


def _pick_graticule_interval_deg(lon_span: float, lat_span: float) -> float:
    target_span = max(lon_span, lat_span)
    for interval in _GRATICULE_INTERVALS_DEG:
        if target_span / interval <= _GRATICULE_TARGET_LINES:
            return interval
    return _GRATICULE_INTERVALS_DEG[-1]


def _build_graticule(map_item: QgsLayoutItemMap, interval_deg: float) -> QgsLayoutItemMapGrid:
    grid = QgsLayoutItemMapGrid("lat/lon graticule", map_item)
    grid.setCrs(_WGS84)
    grid.setIntervalX(interval_deg)
    grid.setIntervalY(interval_deg)
    grid.setStyle(QgsLayoutItemMapGrid.Solid)
    grid.setAnnotationEnabled(True)
    grid.setAnnotationFormat(QgsLayoutItemMapGrid.DegreeMinute)
    grid.setAnnotationPrecision(1)
    # Annotations inside the frame, not outside it -- outside draws into the
    # page margins and collides with the title/footer boxes and the map
    # frame edges (verified visually: default OutsideMapFrame overlapped the
    # title text and got clipped by the page edge on the left).
    for side in (
        QgsLayoutItemMapGrid.Left,
        QgsLayoutItemMapGrid.Right,
        QgsLayoutItemMapGrid.Top,
        QgsLayoutItemMapGrid.Bottom,
    ):
        grid.setAnnotationPosition(QgsLayoutItemMapGrid.InsideMapFrame, side)
    grid.setFrameStyle(QgsLayoutItemMapGrid.Zebra)
    map_item.grids().addGrid(grid)
    return grid


def _build_layout(
    project: QgsProject,
    layer: QgsRasterLayer,
    point_layers: List[QgsVectorLayer],
    line_layers: List[QgsVectorLayer],
    range_ring_layers: List[QgsVectorLayer],
    polygon_layers: List[QgsVectorLayer],
    chart_spec: ChartSpec,
) -> QgsPrintLayout:
    spec = BASEMAPS[chart_spec.chart_type]

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()

    page = layout.pageCollection().pages()[0]
    page.setPageSize(QgsLayoutSize(PAGE_WIDTH_MM, PAGE_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))

    usable_width = PAGE_WIDTH_MM - 2 * MARGIN_MM
    map_top = MARGIN_MM + TITLE_HEIGHT_MM
    map_height = PAGE_HEIGHT_MM - map_top - MARGIN_MM - FOOTER_HEIGHT_MM - SCALEBAR_ZONE_MM

    title = QgsLayoutItemLabel(layout)
    title.setText(chart_spec.title or spec.title)
    text_format = QgsTextFormat()
    text_format.setSize(20)
    title.setTextFormat(text_format)
    layout.addLayoutItem(title)
    title.attemptMove(QgsLayoutPoint(MARGIN_MM, MARGIN_MM, QgsUnitTypes.LayoutMillimeters))
    title.attemptResize(QgsLayoutSize(usable_width, TITLE_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))

    map_item = QgsLayoutItemMap(layout)
    layout.addLayoutItem(map_item)
    map_item.attemptMove(QgsLayoutPoint(MARGIN_MM, map_top, QgsUnitTypes.LayoutMillimeters))
    map_item.attemptResize(QgsLayoutSize(usable_width, map_height, QgsUnitTypes.LayoutMillimeters))
    # draw order: point icons on top, then lines, then range rings, then
    # polygons, then the basemap
    map_item.setLayers(point_layers + line_layers + range_ring_layers + polygon_layers + [layer])
    map_item.setCrs(layer.crs())
    extent = _resolve_extent(chart_spec, spec, layer, usable_width, map_height)
    map_item.zoomToExtent(extent)

    if layer.crs() != _WGS84:
        to_wgs84 = QgsCoordinateTransform(layer.crs(), _WGS84, QgsProject.instance())
        wgs84_extent = to_wgs84.transformBoundingBox(extent)
    else:
        wgs84_extent = extent
    interval_deg = _pick_graticule_interval_deg(wgs84_extent.width(), wgs84_extent.height())
    _build_graticule(map_item, interval_deg)

    _build_north_arrow(layout, MARGIN_MM + usable_width, map_top)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = QgsLayoutItemLabel(layout)
    footer.setText(f"Source: {spec.attribution}  |  Generated {generated_at}  |  nlchart")
    footer_format = QgsTextFormat()
    footer_format.setSize(8)
    footer.setTextFormat(footer_format)
    layout.addLayoutItem(footer)
    footer.attemptMove(
        QgsLayoutPoint(MARGIN_MM, PAGE_HEIGHT_MM - MARGIN_MM - FOOTER_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters)
    )
    footer.attemptResize(QgsLayoutSize(usable_width, FOOTER_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))

    scalebar = QgsLayoutItemScaleBar(layout)
    scalebar.setLinkedMap(map_item)
    scalebar.applyDefaultSize()
    scalebar.setStyle("Single Box")
    layout.addLayoutItem(scalebar)
    scalebar.attemptMove(
        QgsLayoutPoint(MARGIN_MM, map_top + map_height + _SCALEBAR_TOP_GAP_MM, QgsUnitTypes.LayoutMillimeters)
    )

    return layout


def render_chart(chart_spec: ChartSpec, output_path: str) -> str:
    """Render a printable chart PDF for chart_spec to output_path."""
    if chart_spec.vessel_lookups:
        raise ChartRenderError(
            "Vessel/AIS lookups are not yet supported in this phase."
        )

    _ensure_qgis_app()

    project = QgsProject.instance()
    project.clear()

    layer = _add_basemap_layer(project, chart_spec.chart_type)
    point_layers = _add_point_layers(project, chart_spec)
    line_layers = _add_line_layers(project, chart_spec)
    range_ring_layers = _add_range_ring_layers(project, chart_spec)
    polygon_layers = _add_polygon_layers(project, chart_spec)
    layout = _build_layout(
        project, layer, point_layers, line_layers, range_ring_layers, polygon_layers, chart_spec
    )

    exporter = QgsLayoutExporter(layout)
    settings = QgsLayoutExporter.PdfExportSettings()
    settings.dpi = _EXPORT_DPI_BY_TYPE[chart_spec.chart_type]
    result = exporter.exportToPdf(output_path, settings)

    if result != QgsLayoutExporter.ExportResult.Success:
        raise ChartRenderError(f"PDF export for {chart_spec.chart_type!r} failed with code {result}")

    return output_path
