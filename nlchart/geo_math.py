"""Pure spherical-geometry math for chart lines: distances and route
geometry for rhumb lines and great circles. No QGIS or network dependency,
so this is unit-testable in isolation from the rendering pipeline.

Formulas are the standard ones used across aviation/marine navigation
references (haversine great-circle distance, isometric-latitude rhumb-line
distance, spherical slerp for great-circle intermediate points).
"""

import math
from typing import List, Tuple

_EARTH_RADIUS_M = 6_371_000.0  # mean Earth radius (spherical approximation)

# Shared with geocode.py's radius_meters() -- one physical-constants table,
# not two.
UNIT_TO_METERS = {
    "SM": 1609.344,
    "MI": 1609.344,
    "NM": 1852.0,
    "KM": 1000.0,
}


def meters_to_unit(meters: float, unit: str) -> float:
    return meters / UNIT_TO_METERS[unit]


# Values are the literal display strings ("acres", "sq mi", ...), same
# pattern as UNIT_TO_METERS' "SM"/"NM"/"MI"/"KM" already being both the
# validated value and the display text -- no separate display-name mapping
# needed.
AREA_UNIT_TO_M2 = {
    "acres": 4046.8564224,
    "sq mi": 2_589_988.110336,
    "sq km": 1_000_000.0,
    "sq NM": 3_429_904.0,  # 1852**2
    "hectares": 10_000.0,
}


def sq_meters_to_unit(sq_meters: float, unit: str) -> float:
    return sq_meters / AREA_UNIT_TO_M2[unit]


def great_circle_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine (orthodromic / shortest-path) distance in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_M * c


def rhumb_line_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Loxodrome (constant-bearing) distance in meters -- the length of the
    straight line this pair of points draws under a Mercator projection."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    if abs(dlambda) > math.pi:  # shortest way around, not across the antimeridian
        dlambda -= math.copysign(2 * math.pi, dlambda)

    dpsi = math.log(math.tan(math.pi / 4 + phi2 / 2) / math.tan(math.pi / 4 + phi1 / 2))
    q = dphi / dpsi if abs(dpsi) > 1e-12 else math.cos(phi1)  # east-west line: dpsi -> 0
    return math.sqrt(dphi**2 + q**2 * dlambda**2) * _EARTH_RADIUS_M


def great_circle_interpolate(
    lat1: float, lon1: float, lat2: float, lon2: float, n_segments: int
) -> List[Tuple[float, float]]:
    """n_segments+1 points along the great-circle arc from point 1 to point
    2 (inclusive of both endpoints), via spherical slerp. Densifying the arc
    like this is what lets it render as a visibly curved line on a Mercator
    basemap instead of a straight one."""
    phi1, lam1 = math.radians(lat1), math.radians(lon1)
    phi2, lam2 = math.radians(lat2), math.radians(lon2)

    angular_dist = great_circle_distance_m(lat1, lon1, lat2, lon2) / _EARTH_RADIUS_M
    if angular_dist < 1e-12:  # coincident points
        return [(lat1, lon1)] * (n_segments + 1)

    points = []
    for i in range(n_segments + 1):
        f = i / n_segments
        a = math.sin((1 - f) * angular_dist) / math.sin(angular_dist)
        b = math.sin(f * angular_dist) / math.sin(angular_dist)
        x = a * math.cos(phi1) * math.cos(lam1) + b * math.cos(phi2) * math.cos(lam2)
        y = a * math.cos(phi1) * math.sin(lam1) + b * math.cos(phi2) * math.sin(lam2)
        z = a * math.sin(phi1) + b * math.sin(phi2)
        phi_i = math.atan2(z, math.sqrt(x**2 + y**2))
        lam_i = math.atan2(y, x)
        points.append((math.degrees(phi_i), math.degrees(lam_i)))
    return points


def destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> Tuple[float, float]:
    """The direct geodesic problem: the point reached by travelling
    distance_m along a constant true bearing from (lat, lon), on a
    spherical Earth. Standard formula, the natural complement to the
    haversine (inverse) problem above."""
    phi1, lam1 = math.radians(lat), math.radians(lon)
    theta = math.radians(bearing_deg)
    delta = distance_m / _EARTH_RADIUS_M

    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return (math.degrees(phi2), math.degrees(lam2))


def circle_points(lat: float, lon: float, radius_m: float, n_segments: int = 72) -> List[Tuple[float, float]]:
    """A closed ring of n_segments+1 points (first == last) approximating a
    circle of radius_m around (lat, lon) -- used to draw range rings."""
    points = [destination_point(lat, lon, bearing, radius_m) for bearing in
              (360.0 * i / n_segments for i in range(n_segments))]
    points.append(points[0])
    return points


_ARC_SEGMENTS_PER_LEG = 32


def route_distance_m(waypoints: List[Tuple[float, float]], line_type: str) -> float:
    """Total distance across all legs of a multi-waypoint route."""
    distance_fn = great_circle_distance_m if line_type == "great_circle" else rhumb_line_distance_m
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(waypoints, waypoints[1:]):
        total += distance_fn(lat1, lon1, lat2, lon2)
    return total


def route_geometry_points(waypoints: List[Tuple[float, float]], line_type: str) -> List[Tuple[float, float]]:
    """The polyline points to actually draw: waypoints unchanged for a rhumb
    route (each leg is already a straight line under Mercator), or each leg
    densified into a great-circle arc and concatenated."""
    if line_type != "great_circle":
        return list(waypoints)

    points = [waypoints[0]]
    for (lat1, lon1), (lat2, lon2) in zip(waypoints, waypoints[1:]):
        arc = great_circle_interpolate(lat1, lon1, lat2, lon2, _ARC_SEGMENTS_PER_LEG)
        points.extend(arc[1:])  # skip first point -- duplicate of the previous leg's endpoint
    return points
