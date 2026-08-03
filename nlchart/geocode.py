"""Deterministic area resolution: references (coords/landmarks/airports) ->
a WGS84 bounding box.

Kept separate from the LLM call on purpose. Named-place coordinates and
extents come from a real geocoder, never from the model's memorized
training data -- a hallucinated lat/lon is a safety problem for a tool
meant to hand emergency responders a chart they can trust.
"""

import math

import requests

from .geo_math import UNIT_TO_METERS
from .spec import (
    AreaSpec,
    LabeledPoint,
    LineSpec,
    LLMAreaReference,
    LLMAreaSpec,
    LLMLabeledPoint,
    LLMLineSpec,
    LLMPolygonSpec,
    LLMRangeRingSpec,
    PolygonSpec,
    RangeRingSpec,
)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "nlchart/0.1 (c.whittlesey2@gmail.com)"

_METERS_PER_DEGREE_LAT = 111_320.0

# Below this, treat a geocoded result as a point feature (an airport, an
# address) rather than a real region with its own extent -- using its
# "boundingbox" as a chart frame would be near-zero-size and useless.
_MIN_REGION_EXTENT_M = 300.0


class GeocodeError(RuntimeError):
    pass


def radius_meters(value: float, unit: str) -> float:
    try:
        return value * UNIT_TO_METERS[unit]
    except KeyError:
        raise GeocodeError(f"Unknown radius unit {unit!r}")


def _nominatim_lookup(reference_text: str) -> dict:
    try:
        response = requests.get(
            _NOMINATIM_URL,
            # Restricted to the US: both NOAA nautical charts and FAA sectionals
            # only cover US waters/airspace anyway, and without this a common
            # place name can resolve to an unrelated same-named spot elsewhere
            # in the world ("the Everglades" -> a small lake in South Australia
            # was Nominatim's top unrestricted hit).
            params={"q": reference_text, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException as exc:
        raise GeocodeError(f"Geocoding lookup failed for {reference_text!r}: {exc}")

    if not results:
        raise GeocodeError(
            f"Could not find a location for {reference_text!r}. "
            "Try a more specific or differently spelled name."
        )
    return results[0]


def resolve_reference(ref: LLMAreaReference) -> tuple:
    """Return (lat, lon) in WGS84 for a single reference."""
    if ref.reference_type == "coordinates":
        if ref.lat is None or ref.lon is None:
            raise GeocodeError(
                f"Request claimed explicit coordinates for {ref.reference_text!r} "
                "but didn't provide lat/lon."
            )
        return (ref.lat, ref.lon)

    result = _nominatim_lookup(ref.reference_text)
    return (float(result["lat"]), float(result["lon"]))


def _resolve_region_bounds(ref: LLMAreaReference) -> tuple:
    """Return (south, west, north, east) for a named region's own extent.
    Raises GeocodeError if the reference is a point feature (no meaningful
    area) rather than a region -- that case needs an explicit radius, not a
    guess."""
    if ref.reference_type == "coordinates":
        raise GeocodeError(
            f"{ref.reference_text!r} is a bare coordinate, which has no natural "
            "extent of its own -- specify a radius (e.g. '10 SM around ...')."
        )

    result = _nominatim_lookup(ref.reference_text)
    bbox = result.get("boundingbox")
    if not bbox:
        raise GeocodeError(f"No boundary data available for {ref.reference_text!r}.")

    south, north, west, east = (float(x) for x in bbox)
    mean_lat = (south + north) / 2
    lat_span_m = (north - south) * _METERS_PER_DEGREE_LAT
    lon_span_m = (east - west) * _METERS_PER_DEGREE_LAT * math.cos(math.radians(mean_lat))
    diagonal_m = math.hypot(lat_span_m, lon_span_m)

    if diagonal_m < _MIN_REGION_EXTENT_M:
        raise GeocodeError(
            f"{ref.reference_text!r} resolves to a point, not a region with its "
            "own natural extent -- specify a radius (e.g. '10 SM around ...')."
        )
    return (south, west, north, east)


def resolve_area(spec: LLMAreaSpec) -> AreaSpec:
    """Resolve an LLMAreaSpec (references + optional radius) into a plain
    WGS84 bounding box, regardless of which of the three modes it is."""
    if not spec.references:
        raise GeocodeError("Area was requested but no place or coordinates were given.")

    if len(spec.references) >= 2:
        points = [resolve_reference(ref) for ref in spec.references]
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        return AreaSpec(bounds_wgs84=(min(lats), min(lons), max(lats), max(lons)))

    ref = spec.references[0]

    if spec.radius_value is not None:
        if spec.radius_unit is None:
            raise GeocodeError(f"A radius value was given for {ref.reference_text!r} with no unit.")
        lat, lon = resolve_reference(ref)
        meters = radius_meters(spec.radius_value, spec.radius_unit)
        lat_delta = meters / _METERS_PER_DEGREE_LAT
        lon_delta = meters / (_METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)))
        return AreaSpec(bounds_wgs84=(lat - lat_delta, lon - lon_delta, lat + lat_delta, lon + lon_delta))

    south, west, north, east = _resolve_region_bounds(ref)
    return AreaSpec(bounds_wgs84=(south, west, north, east))


def resolve_line(spec: LLMLineSpec) -> LineSpec:
    """Resolve each waypoint reference (place/airport/coordinates -- same
    resolution as area references, no separate lookup needed) into a
    LineSpec carrying plain (lat, lon) waypoints."""
    if len(spec.waypoints) < 2:
        raise GeocodeError(f"Line {spec.label!r} needs at least 2 waypoints to draw anything.")

    waypoints_wgs84 = [resolve_reference(ref) for ref in spec.waypoints]
    return LineSpec(
        label=spec.label,
        waypoints_wgs84=waypoints_wgs84,
        line_type=spec.line_type,
        show_distance=spec.show_distance,
        distance_unit=spec.distance_unit,
        color=spec.color,
    )


def resolve_point(point: LLMLabeledPoint) -> LabeledPoint:
    """Resolve a point's location reference (coordinates, an airport, or a
    named landmark -- same resolution as area/line references) into a
    LabeledPoint carrying a plain (lat, lon)."""
    lat, lon = resolve_reference(point.location)
    return LabeledPoint(label=point.label, lat=lat, lon=lon)


def resolve_polygon(spec: LLMPolygonSpec) -> PolygonSpec:
    """Resolve each vertex reference (place/airport/coordinates -- same
    resolution as area/line/point references) into a PolygonSpec carrying
    plain (lat, lon) vertices, in order."""
    if len(spec.vertices) < 3:
        raise GeocodeError(f"Polygon {spec.label!r} needs at least 3 vertices to draw an area.")

    vertices_wgs84 = [resolve_reference(ref) for ref in spec.vertices]
    return PolygonSpec(
        label=spec.label,
        vertices_wgs84=vertices_wgs84,
        fill_style=spec.fill_style,
        color=spec.color,
        show_area=spec.show_area,
        area_unit=spec.area_unit,
        show_perimeter=spec.show_perimeter,
        perimeter_unit=spec.perimeter_unit,
    )


def resolve_range_rings(spec: LLMRangeRingSpec) -> RangeRingSpec:
    """Resolve a range-ring group's center reference and convert each ring
    distance to meters (reusing radius_meters(), already shared with area-
    radius handling)."""
    if not spec.ring_distances:
        raise GeocodeError(f"Range rings {spec.label!r} need at least one ring distance.")
    if any(d <= 0 for d in spec.ring_distances):
        raise GeocodeError(f"Range rings {spec.label!r} has a non-positive ring distance.")

    center_wgs84 = resolve_reference(spec.center)
    ring_distances_m = sorted(radius_meters(d, spec.distance_unit) for d in spec.ring_distances)
    return RangeRingSpec(
        label=spec.label,
        center_wgs84=center_wgs84,
        ring_distances_m=ring_distances_m,
        distance_unit=spec.distance_unit,
        color=spec.color,
    )
