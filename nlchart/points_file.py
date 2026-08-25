"""Deterministic parsing of an uploaded point list (CSV/TSV or .xlsx) into
(label, lat, lon) rows.

This exists so a list of points never has to be pasted as freeform text and
round-tripped through the LLM's parse call -- that path has a hard ceiling
(the model's structured JSON output is capped at a fixed token budget, so a
long pasted list truncates mid-JSON and crashes) and is vulnerable to
copy/paste artifacts (e.g. a table copied out of a multi-column PDF often
loses its row/column structure). A real file has neither problem: it's
parsed here with plain Python, with no size limit tied to model output and
no LLM interpretation of the coordinates at all.
"""

import csv
import io
from typing import List, Tuple

_LABEL_HEADERS = {"label", "name", "site", "id", "location", "point"}
_LAT_HEADERS = {"lat", "latitude", "y"}
_LON_HEADERS = {"lon", "lng", "long", "longitude", "x"}


class PointsFileError(RuntimeError):
    pass


def parse_points_file(filename: str, content: bytes) -> List[Tuple[str, float, float]]:
    """Returns a list of (label, lat, lon) tuples. Raises PointsFileError on
    anything malformed -- an empty file, unrecognized columns, a
    non-numeric or out-of-range coordinate."""
    if filename.lower().endswith(".xlsx"):
        rows = _read_xlsx_rows(content)
    else:
        rows = _read_delimited_rows(content)

    if not rows:
        raise PointsFileError(f"{filename!r} has no rows.")

    label_idx, lat_idx, lon_idx, data_rows = _locate_columns(filename, rows)

    points = []
    for i, row in enumerate(data_rows, start=1):
        needed = max(i for i in (label_idx, lat_idx, lon_idx) if i is not None)
        if len(row) <= needed:
            raise PointsFileError(f"row {i} of {filename!r} is missing a column.")

        label = str(row[label_idx]).strip() if label_idx is not None else ""
        if not label:
            label = f"Point {i}"

        try:
            lat = float(row[lat_idx])
            lon = float(row[lon_idx])
        except (TypeError, ValueError):
            raise PointsFileError(
                f"row {i} of {filename!r} has a non-numeric latitude/longitude."
            )
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise PointsFileError(
                f"row {i} of {filename!r} has an out-of-range coordinate ({lat}, {lon})."
            )
        points.append((label, lat, lon))

    if not points:
        raise PointsFileError(f"{filename!r} has a header row but no data rows.")

    return points


def _read_delimited_rows(content: bytes) -> List[List[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PointsFileError(
            "file isn't readable as text -- expected a CSV/TSV file, or a .xlsx file."
        )
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel  # comma-delimited fallback
    reader = csv.reader(io.StringIO(text), dialect)
    return [row for row in reader if any(cell.strip() for cell in row)]


def _read_xlsx_rows(content: bytes) -> List[List]:
    import openpyxl  # deferred -- only needed for the .xlsx path

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise PointsFileError(f"could not read xlsx file: {exc}")
    sheet = workbook.worksheets[0]
    rows = []
    for row in sheet.iter_rows(values_only=True):
        if any(cell is not None and str(cell).strip() for cell in row):
            rows.append(["" if cell is None else cell for cell in row])
    return rows


def _locate_columns(filename: str, rows: List[List]):
    """Returns (label_idx_or_None, lat_idx, lon_idx, data_rows)."""
    header = [str(cell).strip().lower() for cell in rows[0]]
    lat_idx = next((i for i, h in enumerate(header) if h in _LAT_HEADERS), None)
    lon_idx = next((i for i, h in enumerate(header) if h in _LON_HEADERS), None)

    if lat_idx is not None and lon_idx is not None:
        label_idx = next((i for i, h in enumerate(header) if h in _LABEL_HEADERS), None)
        if label_idx is None:
            remaining = [i for i in range(len(header)) if i not in (lat_idx, lon_idx)]
            label_idx = remaining[0] if remaining else None
        return label_idx, lat_idx, lon_idx, rows[1:]

    # No recognized lat/lon header -- assume there's no header row at all,
    # just plain columns in label, lat, lon order.
    if len(header) < 3:
        raise PointsFileError(
            f"couldn't find latitude/longitude columns in {filename!r} -- expected "
            "a header row (e.g. \"label,lat,lon\") or 3 plain columns: label, "
            "latitude, longitude."
        )
    return 0, 1, 2, rows
