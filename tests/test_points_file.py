"""Regression tests for nlchart/points_file.py.

Like geo_math.py, this module has no QGIS/network dependency, so it's
tested directly rather than only ever exercised by hand -- it's the layer
that parses whatever file a user uploads, so its column-detection heuristics
and error messages are worth locking in.
"""

import io

import openpyxl
import pytest

from nlchart.points_file import PointsFileError, parse_points_file


def test_csv_with_recognized_header():
    content = b"label,lat,lon\nCamp Alpha,60.62,-147.20\nCamp Bravo,60.58,-147.05\n"
    assert parse_points_file("sites.csv", content) == [
        ("Camp Alpha", 60.62, -147.20),
        ("Camp Bravo", 60.58, -147.05),
    ]


def test_tsv_no_header_positional():
    content = "Camp Alpha\t60.62\t-147.20\nCamp Bravo\t60.58\t-147.05\n".encode()
    assert parse_points_file("sites.tsv", content) == [
        ("Camp Alpha", 60.62, -147.20),
        ("Camp Bravo", 60.58, -147.05),
    ]


def test_alternate_header_names():
    content = b"name,latitude,longitude\nSite 1,61.1,-149.9\n"
    assert parse_points_file("sites.csv", content) == [("Site 1", 61.1, -149.9)]


def test_semicolon_delimiter():
    content = b"label;lat;lon\nA;10.0;20.0\n"
    assert parse_points_file("sites.csv", content) == [("A", 10.0, 20.0)]


def test_lat_lon_header_without_label_column_gets_synthetic_labels():
    content = b"lat,lon\n10.0,20.0\n11.0,21.0\n"
    assert parse_points_file("sites.csv", content) == [
        ("Point 1", 10.0, 20.0),
        ("Point 2", 11.0, 21.0),
    ]


def test_blank_label_cell_gets_synthetic_label():
    content = b"label,lat,lon\n,10.0,20.0\n"
    assert parse_points_file("sites.csv", content) == [("Point 1", 10.0, 20.0)]


def test_xlsx():
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["label", "lat", "lon"])
    sheet.append(["Camp Alpha", 60.62, -147.20])
    buf = io.BytesIO()
    workbook.save(buf)
    assert parse_points_file("sites.xlsx", buf.getvalue()) == [("Camp Alpha", 60.62, -147.20)]


def test_empty_file_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("empty.csv", b"")


def test_header_only_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("sites.csv", b"label,lat,lon\n")


def test_non_numeric_coordinate_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("sites.csv", b"label,lat,lon\nA,not_a_number,-147\n")


def test_out_of_range_latitude_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("sites.csv", b"label,lat,lon\nA,200,-147\n")


def test_out_of_range_longitude_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("sites.csv", b"label,lat,lon\nA,10,200\n")


def test_too_few_columns_without_header_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("sites.csv", b"10.0,20.0\n")


def test_row_missing_a_column_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("sites.csv", b"label,lat,lon\nA,10.0\n")


def test_non_text_content_raises():
    with pytest.raises(PointsFileError):
        parse_points_file("sites.csv", b"\xff\xfe\x00\x01binary garbage")
