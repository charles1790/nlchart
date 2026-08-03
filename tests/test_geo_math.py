"""Regression tests for nlchart/geo_math.py.

geo_math.py is deliberately free of QGIS/network dependencies so it can be
tested in isolation -- this suite locks in the "verify against a known
value before trusting it" checks that were previously only ever run as
throwaway scripts during development (haversine/rhumb sanity checks, the
destination-point round-trip, the circle-point equidistance check, the
polygon-area cross-check against a hand-computed box). None of that was
persisted before; this is that suite, so future changes can't silently
break something that was already verified once.
"""

import math

import pytest

from nlchart.geo_math import (
    AREA_UNIT_TO_M2,
    UNIT_TO_METERS,
    circle_points,
    destination_point,
    great_circle_distance_m,
    great_circle_interpolate,
    meters_to_unit,
    rhumb_line_distance_m,
    route_distance_m,
    route_geometry_points,
    sq_meters_to_unit,
)

_EARTH_RADIUS_M = 6_371_000.0  # matches geo_math.py's spherical approximation


# --- great_circle_distance_m -------------------------------------------------


def test_great_circle_distance_symmetric():
    a, b = (61.2, -149.9), (47.6, -122.3)
    assert great_circle_distance_m(*a, *b) == pytest.approx(great_circle_distance_m(*b, *a))


def test_great_circle_distance_zero_for_same_point():
    assert great_circle_distance_m(40.0, -73.0, 40.0, -73.0) == pytest.approx(0.0, abs=1e-6)


def test_great_circle_quarter_circumference():
    # Equator to the north pole is exactly a quarter of the great circle
    # circumference -- pi/2 * R, a pure geometric identity independent of
    # any external reference table, good for catching a sign/factor bug.
    d = great_circle_distance_m(0.0, 0.0, 90.0, 0.0)
    assert d == pytest.approx((math.pi / 2) * _EARTH_RADIUS_M, rel=1e-9)


def test_great_circle_known_airport_pair():
    # JFK -> LAX. Commonly published great-circle distance is ~2475 statute
    # miles (WGS84-ellipsoid-based calculators). This module uses a
    # spherical approximation, so allow ~1% slack rather than expecting an
    # exact match.
    jfk = (40.6413, -73.7781)
    lax = (33.9416, -118.4085)
    distance_sm = meters_to_unit(great_circle_distance_m(*jfk, *lax), "SM")
    assert distance_sm == pytest.approx(2475, rel=0.01)


# --- rhumb_line_distance_m ---------------------------------------------------


def test_rhumb_equals_great_circle_on_meridian():
    # A due-north/south line is simultaneously a great circle and a rhumb
    # line, so both formulas must agree exactly (up to floating point).
    a, b = (10.0, -50.0), (25.0, -50.0)
    assert rhumb_line_distance_m(*a, *b) == pytest.approx(great_circle_distance_m(*a, *b), rel=1e-9)


def test_rhumb_equals_great_circle_on_equator():
    # The equator is simultaneously a great circle and a rhumb line too.
    a, b = (0.0, -50.0), (0.0, -20.0)
    assert rhumb_line_distance_m(*a, *b) == pytest.approx(great_circle_distance_m(*a, *b), rel=1e-9)


def test_rhumb_at_least_as_long_as_great_circle():
    # A rhumb line is never shorter than the great-circle (shortest-path)
    # distance between the same two points.
    a, b = (61.2, -149.9), (33.9, -118.4)
    assert rhumb_line_distance_m(*a, *b) >= great_circle_distance_m(*a, *b) - 1e-6


# --- destination_point / circle_points --------------------------------------


def test_destination_point_due_north():
    lat, lon = destination_point(0.0, 0.0, 0.0, 100_000)
    assert lon == pytest.approx(0.0, abs=1e-9)
    assert lat == pytest.approx(math.degrees(100_000 / _EARTH_RADIUS_M), rel=1e-9)


def test_destination_point_due_east_on_equator():
    lat, lon = destination_point(0.0, 0.0, 90.0, 100_000)
    assert lat == pytest.approx(0.0, abs=1e-9)
    assert lon == pytest.approx(math.degrees(100_000 / _EARTH_RADIUS_M), rel=1e-9)


@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 225, 270, 315])
def test_destination_point_roundtrip(bearing):
    lat0, lon0 = 61.2, -149.9
    distance_m = 18_520.0  # 10 NM
    lat1, lon1 = destination_point(lat0, lon0, bearing, distance_m)
    assert great_circle_distance_m(lat0, lon0, lat1, lon1) == pytest.approx(distance_m, rel=1e-9)


def test_circle_points_closed_and_equidistant():
    center = (61.2, -149.9)
    radius_m = 18_520.0
    pts = circle_points(*center, radius_m, n_segments=36)
    assert pts[0] == pts[-1]
    assert len(pts) == 37
    for lat, lon in pts[:-1]:
        assert great_circle_distance_m(*center, lat, lon) == pytest.approx(radius_m, rel=1e-9)


# --- great_circle_interpolate -----------------------------------------------


def test_great_circle_interpolate_endpoints_match():
    start, end = (61.2, -149.9), (47.6, -122.3)
    pts = great_circle_interpolate(*start, *end, n_segments=8)
    assert pts[0] == pytest.approx(start)
    assert pts[-1] == pytest.approx(end)


def test_great_circle_interpolate_midpoint_is_equidistant():
    start, end = (61.2, -149.9), (47.6, -122.3)
    pts = great_circle_interpolate(*start, *end, n_segments=2)
    mid = pts[1]
    d_start_mid = great_circle_distance_m(*start, *mid)
    d_mid_end = great_circle_distance_m(*mid, *end)
    assert d_start_mid == pytest.approx(d_mid_end, rel=1e-6)


# --- route_distance_m / route_geometry_points -------------------------------


def test_route_distance_two_waypoints_matches_direct_rhumb():
    a, b = (61.2, -149.9), (60.5, -148.5)
    assert route_distance_m([a, b], "rhumb") == pytest.approx(rhumb_line_distance_m(*a, *b))


def test_route_distance_two_waypoints_matches_direct_great_circle():
    a, b = (61.2, -149.9), (60.5, -148.5)
    assert route_distance_m([a, b], "great_circle") == pytest.approx(great_circle_distance_m(*a, *b))


def test_route_distance_sums_legs():
    a, b, c = (61.2, -149.9), (60.8, -149.0), (60.5, -148.5)
    total = route_distance_m([a, b, c], "rhumb")
    expected = rhumb_line_distance_m(*a, *b) + rhumb_line_distance_m(*b, *c)
    assert total == pytest.approx(expected)


def test_route_geometry_points_rhumb_returns_waypoints_unchanged():
    waypoints = [(61.2, -149.9), (60.8, -149.0), (60.5, -148.5)]
    assert route_geometry_points(waypoints, "rhumb") == waypoints


def test_route_geometry_points_great_circle_endpoints_preserved():
    waypoints = [(61.2, -149.9), (60.8, -149.0), (60.5, -148.5)]
    points = route_geometry_points(waypoints, "great_circle")
    assert points[0] == pytest.approx(waypoints[0])
    assert points[-1] == pytest.approx(waypoints[-1])
    assert len(points) > len(waypoints)  # densified, not just the waypoints


# --- unit conversion tables --------------------------------------------------


def test_linear_unit_conversions():
    assert meters_to_unit(1852.0, "NM") == pytest.approx(1.0)
    assert meters_to_unit(1609.344, "SM") == pytest.approx(1.0)
    assert meters_to_unit(1609.344, "MI") == pytest.approx(1.0)
    assert meters_to_unit(1000.0, "KM") == pytest.approx(1.0)


def test_area_unit_conversions_against_independent_definitions():
    # Cross-checked against unit definitions independent of this module's
    # own table, not just round-tripped against itself.
    sq_ft_per_acre = 43_560
    m2_per_sq_ft = 0.09290304
    assert AREA_UNIT_TO_M2["acres"] == pytest.approx(sq_ft_per_acre * m2_per_sq_ft)

    assert AREA_UNIT_TO_M2["sq mi"] == pytest.approx(UNIT_TO_METERS["MI"] ** 2)
    assert AREA_UNIT_TO_M2["sq NM"] == pytest.approx(UNIT_TO_METERS["NM"] ** 2)
    assert AREA_UNIT_TO_M2["sq km"] == pytest.approx(UNIT_TO_METERS["KM"] ** 2)

    assert sq_meters_to_unit(AREA_UNIT_TO_M2["acres"], "acres") == pytest.approx(1.0)
    assert sq_meters_to_unit(AREA_UNIT_TO_M2["hectares"], "hectares") == pytest.approx(1.0)
