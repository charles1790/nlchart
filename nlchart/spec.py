"""Structured chart-request schema.

Two tiers, deliberately kept separate:

- LLMChartRequest (+ LLMAreaSpec/LLMPointSetSpec/LLMLabeledPoint/LLMLineSpec/
  LLMPolygonSpec) -- what we actually ask the model to extract this phase:
  chart_type, area, labeled point sets, labeled route lines, and
  NL-vertex-defined polygons. Small and explicit on purpose: the model should
  never be asked to fill in fields we don't yet act on.
- ChartSpec (+ AreaSpec/PointSetSpec/LabeledPoint/LineSpec/PolygonSpec, and
  stub VesselLookupSpec) -- the resolved domain model render.py consumes.
  Polygon vertices are NL-defined (place names/coordinates), not shapefile
  imports -- shapefile-derived polygons are a distinct, deferred feature.
  vessel_lookups stays a stub field so that later phase is additive instead
  of a redesign, but render.py rejects it if populated -- nothing downstream
  implements it yet.
"""

from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel

# Small fixed palette rather than freeform color strings/hex: keeps the LLM's
# output constrained to values render.py actually knows how to draw, and
# matches how a human would ask ("a green dot", not "a dot in #3cb44b").
PointColor = Literal["red", "green", "blue", "yellow", "orange", "black", "white", "purple"]

# Matches the actual SVG assets render.py has available (QGIS's bundled
# icon library). "dot" is the plain circle marker -- the default when
# nothing in the request implies a more specific icon.
PointIcon = Literal["dot", "boat", "helicopter", "plane", "car", "flag", "house", "anchor"]

DistanceUnit = Literal["SM", "NM", "MI", "KM"]

# Rhumb (constant compass bearing -- a straight line under this project's
# Mercator basemaps) is the navigation default; great_circle (shortest path,
# renders as a bowed arc) is opt-in only on an explicit ask.
LineType = Literal["rhumb", "great_circle"]


class LLMAreaReference(BaseModel):
    reference_text: str
    reference_type: Literal["airport", "named_place", "coordinates"]
    lat: Optional[float] = None
    lon: Optional[float] = None


class LLMAreaSpec(BaseModel):
    # One reference + a radius -> circle-ish buffer around a point.
    # One reference, no radius -> that place's own natural extent (a bay,
    #   refuge, park, city, ...); geocode.py rejects this if the place turns
    #   out to be a point feature with no real extent (an airport, an
    #   address) rather than guessing a radius.
    # Two or more references, no radius -> the bounding box containing all
    #   of them (opposite corners, "from X to Y", a mix of landmarks and
    #   explicit coordinates).
    references: List[LLMAreaReference]
    radius_value: Optional[float] = None
    radius_unit: Optional[DistanceUnit] = None


class LLMLabeledPoint(BaseModel):
    label: str
    # Same reference mechanism as area/line waypoints -- explicit
    # coordinates, an airport, or a named landmark ("place an anchor on New
    # York City"). A landmark resolves to that place's geocoded center
    # point, not a precise hand-picked spot; good enough for a general
    # marker, not guaranteed pixel-perfect.
    location: LLMAreaReference


class LLMPointSetSpec(BaseModel):
    color: PointColor
    icon: PointIcon = "dot"
    points: List[LLMLabeledPoint]


class LLMLineSpec(BaseModel):
    label: str
    # >=2, in order; reuses the same reference extraction rules as area
    # (place/airport/coordinates) -- no separate lookup mechanism needed.
    waypoints: List[LLMAreaReference]
    line_type: LineType = "rhumb"
    show_distance: bool = True
    distance_unit: DistanceUnit = "NM"
    color: PointColor = "red"


# "unfilled" is the default: an outline-only polygon reads as a boundary
# marker (a search sector, an exclusion zone) without obscuring the basemap
# underneath, which is the more common use case than a solid block of color.
PolygonFillStyle = Literal["filled", "unfilled", "shaded"]


AreaUnit = Literal["acres", "sq mi", "sq km", "sq NM", "hectares"]


class LLMPolygonSpec(BaseModel):
    label: str
    # >=3, in order; reuses the same reference extraction rules as area
    # corners, line waypoints, and point locations -- named places, airports,
    # and explicit coordinates can mix freely as vertices.
    vertices: List[LLMAreaReference]
    fill_style: PolygonFillStyle = "unfilled"
    color: PointColor = "red"
    # Independently toggleable -- a request can ask for just one, the
    # other, neither, or both. Acres/NM are sensible defaults (SAR/land-
    # search-area convention), same "cosmetic default" treatment as color.
    show_area: bool = True
    area_unit: AreaUnit = "acres"
    show_perimeter: bool = True
    perimeter_unit: DistanceUnit = "NM"


class LLMRangeRingSpec(BaseModel):
    label: str
    # Same reference mechanism as every other location in this schema.
    center: LLMAreaReference
    # e.g. [5, 10, 15, 20] -- distances out from center, one ring each.
    ring_distances: List[float]
    distance_unit: DistanceUnit = "NM"
    color: PointColor = "red"


class LLMChartRequest(BaseModel):
    chart_type: Optional[Literal["nautical", "sectional", "satellite", "topo"]] = None
    # Only set when the request itself names the chart/mission/operation
    # ("call this chart X", "for Operation Y"). Falls back to the basemap's
    # default type title (e.g. "VFR Sectional Chart") when null -- never
    # invented.
    title: Optional[str] = None
    area: Optional[LLMAreaSpec] = None
    point_sets: List[LLMPointSetSpec] = []
    lines: List[LLMLineSpec] = []
    polygons: List[LLMPolygonSpec] = []
    range_rings: List[LLMRangeRingSpec] = []
    # Set by the model instead of guessing when the request is ambiguous or
    # missing information it needs (e.g. no chart type, an area reference it
    # isn't confident it can resolve, or a coordinate table it can't parse
    # cleanly). A wrong guessed coordinate is worse than an explicit refusal
    # for this tool.
    clarification_needed: Optional[str] = None


class AreaSpec(BaseModel):
    # (south, west, north, east) in decimal degrees -- whichever of the three
    # modes produced it, area resolution always converges to a plain WGS84
    # bounding box by the time it reaches render.py.
    bounds_wgs84: Tuple[float, float, float, float]


class LabeledPoint(BaseModel):
    label: str
    lat: float
    lon: float


class PointSetSpec(BaseModel):
    color: PointColor
    icon: PointIcon = "dot"
    points: List[LabeledPoint]


class LineSpec(BaseModel):
    label: str
    waypoints_wgs84: List[Tuple[float, float]]  # >=2 resolved (lat, lon), in order
    line_type: LineType
    show_distance: bool
    distance_unit: DistanceUnit
    color: PointColor


class PolygonSpec(BaseModel):
    label: str
    vertices_wgs84: List[Tuple[float, float]]  # >=3 resolved (lat, lon), in order
    fill_style: PolygonFillStyle
    color: PointColor
    show_area: bool
    area_unit: AreaUnit
    show_perimeter: bool
    perimeter_unit: DistanceUnit


class VesselLookupSpec(BaseModel):
    """Placeholder for a later phase (live AIS lookups). Not yet implemented."""

    vessel_name: str = ""


class RangeRingSpec(BaseModel):
    label: str
    center_wgs84: Tuple[float, float]
    ring_distances_m: List[float]  # resolved, sorted ascending
    distance_unit: DistanceUnit  # for display only -- rings are stored in meters
    color: PointColor


class ChartSpec(BaseModel):
    chart_type: Literal["nautical", "sectional", "satellite", "topo"]
    title: Optional[str] = None
    area: Optional[AreaSpec] = None
    point_sets: List[PointSetSpec] = []
    lines: List[LineSpec] = []
    polygons: List[PolygonSpec] = []
    range_rings: List[RangeRingSpec] = []
    vessel_lookups: List[VesselLookupSpec] = []
