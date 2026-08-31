"""LLM-backed NL parser: free text -> validated ChartSpec.

The model's only job is structured extraction: chart_type, title, area, and
any point/line/polygon/range-ring overlays. It never touches QGIS or
generates code; render.py stays in full control of what actually gets
drawn. Named-place coordinates are resolved by a real geocoder (see
geocode.py), not the model's memorized training data.
"""

from typing import List, Optional, Tuple

import anthropic
import pydantic

from ..geocode import resolve_area, resolve_line, resolve_point, resolve_polygon, resolve_range_rings
from ..spec import ChartSpec, LabeledPoint, LLMChartRequest, PointSetSpec
from .base import ParseError

_MODEL = "claude-sonnet-5"

_SYSTEM_PROMPT = """\
You turn a short natural-language request for a printable chart into a structured spec.

Valid chart_type values: "nautical" (marine chart), "sectional" (FAA VFR \
aeronautical chart), "satellite" (satellite imagery), "topo" (USGS \
topographic quad-style map -- contours, trails, terrain shading; the right \
choice for land/mountain search-and-rescue requests).

If the request names or titles the chart itself -- a mission/operation name, \
"call this chart X", "title it Y", "for Operation Z" -- extract it as \
`title`. Leave `title` null if nothing in the request names the chart; do \
NOT invent a title from the area or chart type (a sensible default is \
applied automatically downstream).

If an area/boundary is mentioned, extract it as `area` with a `references` \
list (one or more places/coordinates). Each reference has:
- reference_text: the raw place name or description as the user wrote it \
(e.g. "Anchorage International Airport", "Prince William Sound").
- reference_type: "airport" if it clearly names an airport, "coordinates" \
only if the user gave explicit numeric latitude/longitude, otherwise \
"named_place".
- lat / lon: ONLY set these if reference_type is "coordinates" and the user \
gave explicit numbers. Never invent coordinates for a named place -- leave \
lat/lon null and let reference_text carry the name; a separate lookup \
resolves it precisely afterward.

Common place names exist in many US states (there's a "Whittier" in both \
Alaska and California, a "Springfield" in a dozen states). This applies to \
every reference_text anywhere in this spec -- area boundaries, line \
waypoints, and point locations all resolve the same way. The geocoder does \
an exact text lookup with no awareness of the rest of the request, so if \
the request's context makes the intended one obvious -- another reference \
in the same request is in a particular state or region, a chart type or \
landmark implies it -- make reference_text specific enough to disambiguate \
(e.g. "Whittier, Alaska" rather than bare "Whittier" when the request is \
otherwise clearly about Prince William Sound). Only do this when context \
genuinely points to one; don't invent a state for a name that's already \
unambiguous.

Three ways an area can be expressed -- pick based on what the user actually said:
1. One reference + a distance ("50 SM around X", "within 10 NM of Y"): put \
one entry in `references` and set `radius_value`/`radius_unit` (NM=nautical \
miles, SM=statute miles, MI=plain "miles" with no further qualifier, \
KM=kilometers).
2. One reference, no distance, naming a place that has its own natural \
extent -- a bay, sound, refuge, park, lake, city, or similar named region \
("chart of Prince William Sound", "map the wildlife refuge"): put one entry \
in `references` and leave radius_value/radius_unit null. The place's own \
boundary is looked up automatically -- do not invent a radius for these. \
(If the place turns out to be a point with no mapped boundary, that surfaces \
as a downstream error asking for a radius -- not something to predict here.)
3. Two or more references that together define the coverage area ("from the \
airport to the harbor", corners of the chart, "covering both X and Y", a \
pair of opposite-corner coordinates): list all of them in `references` \
(landmarks, airports, and explicit coordinates can mix freely) and leave \
radius_value/radius_unit null. The chart frames to contain all of them.

If no area is mentioned, leave `area` null entirely -- do not invent one.

If the request asks to place labeled and/or colored dots/points/icons -- at \
specific coordinates (a pasted table, a CSV-like list, a markdown table, or \
an inline list like "Camp Alpha (61.21, -149.90), Camp Bravo (61.25, \
-149.85)"), or on named landmarks ("place an anchor on New York City", \
"mark Merrill Field with a plane icon") -- extract each such group as one \
entry in `point_sets`:
- color: one of red, green, blue, yellow, orange, black, white, purple. If \
the user names a color for the group, use it. If they don't name one, pick \
"red" as a reasonable default -- color choice here is cosmetic, not a fact \
worth refusing over.
- icon: one of dot, boat, helicopter, plane, car, flag, house, anchor. Pick \
whichever matches what the point represents, from an explicit request \
("boat icons", "mark it with a helicopter") or a clear implication in the \
labels/context (a vessel's last known position -> boat; a landing zone or \
medevac site -> helicopter; a downed/crashed aircraft or airstrip marker -> \
plane; a command post, camp, or generic location -> house or flag as fits; \
an anchorage -> anchor). If nothing implies a specific icon, use "dot" -- \
this is a cosmetic default like color, not worth asking about.
- points: each point's `label` (use the name/id given in a table, or a \
short description of the landmark, e.g. "New York City"), and `location` -- \
a reference using the exact same reference_text/reference_type/lat/lon \
rules as area references above. Use "coordinates" (converting DMS or other \
formats to decimal degrees), "airport", or "named_place" as fits; explicit \
coordinates and landmark references can mix freely within one point_sets \
entry. Points in the same request that share both a color and an icon \
belong in the same point_sets entry; a request naming different colors or \
icons for different groups of points becomes multiple point_sets entries.

If the request asks to draw a line, route, course, or track on the chart \
and label it -- "draw a line from X to Y", "show the route from A to B to \
C", "plot a course to Z" -- extract each such line as one entry in `lines`:
- label: a short name/description, from what the user called it if they \
named it (e.g. "Patrol Route Alpha"), otherwise a reasonable short \
description of what it connects (e.g. "Anchorage to Whittier").
- waypoints: 2 or more references in order, using the exact same \
reference_text/reference_type/lat/lon rules (and the same disambiguation \
guidance above) as area references -- place names, airports, and explicit \
coordinates all work and can mix within one line.
- line_type: "rhumb" (constant compass bearing -- the navigation default, \
and a straight line on this tool's charts) unless the request explicitly \
asks for a "great circle", "geodesic", or "shortest path" route, in which \
case use "great_circle".
- show_distance: true unless the request explicitly says not to show \
distance.
- distance_unit: "NM" by default; honor SM/MI/KM if the request asks for a \
specific unit.
- color: same 8-color palette as points; default "red" if not stated -- \
cosmetic, not worth refusing over.

If the request asks to draw and label a polygon, area, zone, sector, or \
boundary defined by a list of corners/vertices -- "outline a search sector \
with corners at X, Y, Z", "shade the exclusion zone bounded by A, B, C, D" \
-- extract each such shape as one entry in `polygons`:
- label: a short name/description, from what the user called it if named \
(e.g. "Search Sector 1"), otherwise a reasonable short description.
- vertices: 3 or more references in order, using the exact same \
reference_text/reference_type/lat/lon rules (and the same disambiguation \
guidance above) as area references, line waypoints, and point locations -- \
place names, airports, and explicit coordinates all work and can mix within \
one polygon.
- fill_style: "unfilled" (outline only) unless the request implies \
otherwise -- "filled" for a solid block of color ("fill it in", "solid \
red area"), "shaded" for a lighter hatched fill that still lets the \
basemap show through ("shade the zone", "hatch the area"). Default to \
"unfilled" if nothing implies a fill.
- color: same 8-color palette as points/lines, default "red" if not \
stated -- cosmetic, not worth refusing over.
- show_area / show_perimeter: both true by default -- the polygon's label \
shows its area and perimeter automatically. Set either to false only if \
the request explicitly says not to show it ("don't show the area", "no \
perimeter", "just the perimeter" implies show_area=false). All four \
combinations (area only, perimeter only, neither, both) are valid.
- area_unit: "acres" by default; honor "sq mi"/"sq km"/"sq NM"/"hectares" \
if the request names a specific unit for the area.
- perimeter_unit: "NM" by default; honor SM/MI/KM if the request names a \
specific unit for the perimeter (this is the same DistanceUnit as lines).

This tool does NOT support importing polygons from a shapefile or other \
GIS file -- only polygons whose vertices are given directly in the request \
(named places, airports, or coordinates) are supported. If a request asks \
to import/load/use a shapefile or other external file for a boundary, \
treat that as unsupported (see below) rather than extracting it as a \
`polygons` entry.

If the request asks for range rings, distance rings, or search rings \
around a point -- concentric circles at set distances, typically for a \
search-and-rescue expanding search pattern ("range rings every 5 NM out to \
20 NM from the last known position", "search rings at 10 and 20 NM around \
Merrill Field") -- extract each such group as one entry in `range_rings`:
- label: a short name/description (e.g. "Search rings from LKP").
- center: one reference, using the exact same reference_text/reference_type/\
lat/lon rules as everywhere else in this spec.
- ring_distances: the list of distances from the request, in the order/\
values implied ("every 5 NM out to 20 NM" -> [5, 10, 15, 20]; "at 10 and 20 \
NM" -> [10, 20]). At least one distance is required.
- distance_unit: "NM" by default; honor SM/MI/KM if stated.
- color: same 8-color palette as everything else, default "red".

If the request does not clearly specify one of the four valid chart types, \
or mentions something this tool cannot yet do (importing a shapefile or \
other GIS file, vessel/AIS lookups -- if the request already has \
coordinates/positions to plot, e.g. a pasted table, extract those as \
`point_sets` instead of refusing), or gives a coordinate table, line, \
polygon, or range-ring group that's too malformed or ambiguous to extract \
cleanly, do NOT guess -- set `clarification_needed` to a short explanation \
of what's missing or unsupported, and leave chart_type/area/point_sets/\
lines/polygons/range_rings at their defaults.
"""


_FILE_ADDENDUM = """

A separate file of labeled points has been attached to this request. Its \
coordinates are NOT in the text below -- they've already been parsed \
directly from the file, not by you. If the request describes how to style \
those points (a color, an icon), extract exactly one `point_sets` entry \
with that color/icon and an empty `points` list -- the real points get \
filled in downstream. If the request says nothing about styling them, \
leave `point_sets` empty entirely; a default is applied downstream. Do NOT \
invent any points/coordinates of your own for this request; every \
`point_sets` entry you produce must have an empty `points` list.
"""


class ClaudeParser:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic()

    def parse(
        self, text: str, uploaded_points: Optional[List[Tuple[str, float, float]]] = None
    ) -> ChartSpec:
        system = _SYSTEM_PROMPT + (_FILE_ADDENDUM if uploaded_points else "")
        try:
            response = self._client.messages.parse(
                model=_MODEL,
                # Raised from the original 1024 -- that ceiling turned out to
                # bite on any sufficiently rich single request (several
                # overlays with real labels/coordinates in one message), not
                # just long point lists (that case now has a real fix: the
                # points-file upload path bypasses this entirely). 4096 gives
                # roughly 4x the headroom, and a truncated response still
                # fails cleanly below rather than crashing with a raw JSON
                # parse error.
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": text}],
                output_format=LLMChartRequest,
            )
        except pydantic.ValidationError:
            raise ParseError(
                f"Could not parse request {text!r} -- it may be too long or too "
                "detailed for one request. Try shortening it, splitting it into "
                "multiple requests, or (for a long list of points) uploading a "
                "points file instead of pasting them inline."
            )
        parsed = response.parsed_output
        if parsed is None:
            raise ParseError(f"Could not parse request {text!r} into a chart spec.")
        if parsed.clarification_needed:
            raise ParseError(parsed.clarification_needed)
        if parsed.chart_type is None:
            raise ParseError(
                f"Could not determine a chart type from {text!r}. "
                "Ask for a nautical, sectional, satellite, or topo chart."
            )

        area = resolve_area(parsed.area) if parsed.area is not None else None

        point_sets = [
            PointSetSpec(
                color=ps.color,
                icon=ps.icon,
                points=[resolve_point(p) for p in ps.points],
            )
            for ps in parsed.point_sets
        ]

        if uploaded_points is not None:
            # The model was told not to invent coordinates for this request
            # (see _FILE_ADDENDUM) -- at most it named a color/icon via one
            # empty-points entry. Use that styling (or the same defaults
            # PointSetSpec/LLMPointSetSpec use elsewhere) and drop whatever
            # it produced in favor of the real, file-parsed points.
            color = point_sets[0].color if point_sets else "red"
            icon = point_sets[0].icon if point_sets else "dot"
            point_sets = [
                PointSetSpec(
                    color=color,
                    icon=icon,
                    points=[LabeledPoint(label=label, lat=lat, lon=lon) for label, lat, lon in uploaded_points],
                )
            ]

        lines = [resolve_line(ls) for ls in parsed.lines]
        polygons = [resolve_polygon(ps) for ps in parsed.polygons]
        range_rings = [resolve_range_rings(rs) for rs in parsed.range_rings]

        return ChartSpec(
            chart_type=parsed.chart_type,
            title=parsed.title,
            area=area,
            point_sets=point_sets,
            lines=lines,
            polygons=polygons,
            range_rings=range_rings,
        )
