# nlchart

Natural language in, printable professional chart out. Built for emergency
services staff who need a usable map product now, not after someone who knows
QGIS is free to make one.

This is a standalone PyQGIS script (no running QGIS instance required) that
uses the local QGIS 3.44 Python bindings headlessly.

## Phase 3 (current)

Understands chart type (nautical, sectional, satellite, or USGS topo), a
flexible geographic area, labeled/colored/iconed point overlays pasted or
described inline as a table, labeled route lines (rhumb or great circle,
with distance), labeled polygons defined by a list of vertices
(filled/unfilled/shaded, with area/perimeter measurements), and range rings
around a point.
Every chart gets a title (custom or defaulted), a generation timestamp, a
north arrow, and a lat/lon graticule. Shapefile-imported polygons are
typed into the schema already (`nlchart/spec.py`) but not implemented; a
request that needs one is rejected with a clear error rather than silently
ignored. Live AIS/ADSB position lookups are deliberately not planned at
all -- see the Roadmap section.

```
python3 -m nlchart.cli "get me a nautical chart"
python3 -m nlchart.cli "get me a sectional chart of the area 50 SM around Anchorage airport"
python3 -m nlchart.cli "satellite image of Chesapeake Bay"
python3 -m nlchart.cli "sectional chart covering the area from Anchorage International Airport to Whittier, near Prince William Sound"
python3 -m nlchart.cli "get me a nautical chart of Prince William Sound with green labeled dots at the coordinates in this table:
Camp Alpha, 60.62, -147.20
Camp Bravo, 60.58, -147.05
LZ Charlie, 60.65, -147.30"
python3 -m nlchart.cli "sectional chart with a route from Anchorage International Airport through Girdwood, Alaska to Whittier, Alaska, labeled Patrol Loop"
python3 -m nlchart.cli "satellite image with an anchor placed on New York City"
python3 -m nlchart.cli "sectional chart of Anchorage with a shaded exclusion zone bounded by Merrill Field, Elmendorf AFB, and Ted Stevens Anchorage International Airport"
python3 -m nlchart.cli "sectional chart of the area 30 SM around Merrill Field, call it Sector 4 Search, with range rings every 5 NM out to 20 NM from the airport"
python3 -m nlchart.cli "satellite image with a filled red search sector with corners at 61.20,-149.95, 61.20,-149.85, 61.15,-149.90, labeled Search Sector Alpha, show the area in square miles"
python3 -m nlchart.cli "topo map for a mountain rescue search around Mount Rainier, within 5 miles, with search rings every 2 miles out to 6 miles from the last known position at 46.8523,-121.7603 and a helicopter icon marking the LZ at 46.86,-121.74"
```

Add `-o path/to/file.pdf` to control the output path (defaults to
`output/<type>.pdf`).

### Lines -- rhumb or great circle, with distance

A `lines` entry (`nlchart/spec.py::LineSpec`) is a labeled route through 2+
waypoints (`nlchart/geo_math.py` has the actual math, kept dependency-free
and unit-tested against a known airport-pair distance before it was trusted):

- **Waypoints** resolve through the exact same reference mechanism as area
  corners (`nlchart/geocode.py::resolve_reference`) -- place names, airports,
  and explicit coordinates, freely mixed, 2 or more in order. A route with
  3+ waypoints is just N-1 legs chained together; each rhumb leg has its own
  constant bearing, matching how multi-leg navigation is actually planned.
- **`line_type`** is `rhumb` by default (a straight line under this
  project's Mercator basemaps, "for free") and only becomes `great_circle`
  on an explicit ask ("great circle", "geodesic", "shortest path"). A great
  circle leg is densified into ~32 interpolated points
  (`great_circle_interpolate`) so it renders as a visibly bowed arc rather
  than a straight line.
- **Distance** is shown by default, computed with the formula matching
  `line_type` (haversine for great circle, isometric-latitude/meridional-parts
  for rhumb -- these are not the same number; rhumb is always slightly
  longer except along the equator or a meridian) and summed across all legs
  into one total. One label per route (name + total distance), not one per
  leg -- avoids a total distance looking like it belongs to a single
  segment.
- Line-only requests (no area, no points) auto-fit the frame to the route,
  using the full densified arc for a great circle so the bow isn't clipped.

### Polygons -- vertex-defined, filled / unfilled / shaded

A `polygons` entry (`nlchart/spec.py::PolygonSpec`) is a labeled area
defined by 3 or more vertices given directly in the request, in order:

- **Vertices** resolve through the exact same reference mechanism as area
  corners, line waypoints, and point locations
  (`nlchart/geocode.py::resolve_reference`) -- place names, airports, and
  explicit coordinates, freely mixed. This is deliberately vertex-based
  only, not shapefile import -- loading polygons from an external GIS file
  is a distinct, larger feature that's deferred; a request that asks for it
  is rejected with a clear message rather than attempted.
- **`fill_style`** is one of `filled` (solid color), `unfilled` (outline
  only -- the default, since a boundary marker that doesn't obscure the
  basemap underneath is the more common case), or `shaded` (a diagonal
  hatch pattern light enough that basemap detail still shows through).
  Styled with QGIS's `QgsSimpleFillSymbolLayer` + a real `Qt.BrushStyle`
  enum value (`Qt.SolidPattern` / `Qt.NoBrush` / `Qt.FDiagPattern`) rather
  than `QgsFillSymbol.createSimple()`'s string-key shortcuts -- testing
  found several of those string keys (`b_diagonal`, `dense6`/`dense7`)
  silently render the wrong color, while the enum-based approach is
  reliable.
- Polygon-only requests (no area, no points, no lines) auto-fit the frame
  to the polygon's vertices, same as points and lines.
- Z-order: points on top, then lines, then polygons, then the basemap --
  the broadest overlay type stays furthest back.
- **Area and perimeter** are labeled by default (`"Search Sector Alpha --
  5.8 sq mi, 9.6 NM"`), independently toggleable -- a request can ask for
  just one, the other, neither, or both (`show_area`/`show_perimeter` in
  `nlchart/spec.py::PolygonSpec`). Defaults are acres for area (the common
  SAR/land-search-area convention) and NM for perimeter (matching lines).
  Both are computed geodesically at render time
  (`nlchart/render.py::_measure_polygon_geodesic`, `QgsDistanceArea` with
  the WGS84 ellipsoid) rather than a hand-rolled spherical formula --
  verified against a manually-computed box before being trusted, the same
  "verify before trusting" bar every other formula in this project has
  cleared.

### Range rings -- concentric distance circles around a point

A `range_rings` entry (`nlchart/spec.py::RangeRingSpec`) draws one dashed
ring per requested distance around a single center reference -- built for
SAR expanding-search-pattern planning ("range rings every 5 NM out to 20 NM
from the last known position").

- **Center** resolves through the same reference mechanism as everything
  else in this schema (`nlchart/geocode.py::resolve_reference`).
- **Ring geometry** comes from `nlchart/geo_math.py::circle_points`, built
  on `destination_point` (the direct geodesic problem -- the natural
  complement to the great-circle-distance/haversine formula already used
  for lines, and verified against it: computing a destination point and
  then the distance back to it round-trips to the original input distance
  to sub-meter precision).
- Rendered dashed (`line_style: "dash"`), distinct from route lines
  (solid) -- a ring is a distance marker/planning aid, not a navigable
  path. Each ring is labeled with its distance, curved along the ring.
- Range-ring-only requests auto-fit the frame to the outermost ring, same
  as points/lines/polygons.
- Z-order: points, then lines, then range rings, then polygons, then the
  basemap.

### Chart layout: title, timestamp, north arrow, graticule

Every rendered chart (`nlchart/render.py::_build_layout`) now includes:

- **Title**: the request can name the chart/mission/operation directly
  ("call this chart X", "for Operation Y") via `ChartSpec.title`; if
  nothing names it, falls back to the basemap type's default title (e.g.
  "VFR Sectional Chart"), same as before this phase.
- **Generation timestamp**: a UTC date/time in the footer, computed at
  render time (not LLM-extracted -- it's a deterministic fact). Matters
  because basemap tile freshness is unknown and search situations evolve
  in real time, so knowing exactly when a chart was generated is
  operationally relevant.
- **North arrow**: a static, always-up-pointing icon (`_build_north_arrow`)
  from QGIS's bundled `svg/arrows/NorthArrow_04.svg`. Static rather than
  rotation-linked because none of this tool's charts ever rotate the map.
- **Lat/lon graticule**: a `QgsLayoutItemMapGrid` forced to WGS84
  (`_build_graticule`) regardless of the basemap's native projected CRS, so
  labels always read as real lat/lon. The interval is picked from a fixed
  "nice numbers" list (`_pick_graticule_interval_deg`) targeting ~6 grid
  lines across the frame -- QGIS has no built-in auto-interval on this API
  surface, so this is deterministic, not relied-upon undocumented behavior.
  Annotations are drawn **inside** the map frame
  (`QgsLayoutItemMapGrid.InsideMapFrame`), not outside -- outside placement
  was tried first and visually collided with the title/footer boxes and
  got clipped at the page edge, since the map frame sits nearly flush
  against them on this page layout.

### Area specification -- three modes, one WGS84 bounding box

`AreaSpec` always resolves down to a plain `(south, west, north, east)`
bounding box (`nlchart/spec.py`) regardless of which of three ways the user
expressed it (`nlchart/geocode.py::resolve_area`):

1. **Radius around one point** -- "50 SM around Anchorage airport". A place
   name, airport, or explicit coordinates, plus a distance.
2. **A named region's own extent** -- "chart of Chesapeake Bay", no distance
   given. Uses the place's real OpenStreetMap boundary via Nominatim's
   `boundingbox`, not a guessed radius. This depends on how well the place
   happens to be mapped in OSM: administrative areas, parks, refuges, and
   well-known bays are usually mapped as real polygons and work well;
   smaller or less-documented features are sometimes only a label point with
   no real boundary, in which case this mode fails on purpose (asks for a
   radius) rather than rendering an arbitrary guessed extent. Some airports
   (Denver Intl, e.g.) are large enough to have a real mapped property
   boundary and work here too, without a radius.
3. **Two or more references defining the coverage area** -- "from the
   airport to the harbor", opposite corners, a mix of landmarks and explicit
   coordinates. Frames to contain all of them.

Since (1) still needs a real ground radius and (3) needs true corner
coordinates, both convert to the bounding box via the same projected-space
math the point-auto-fit path uses (`_bbox_center_and_scale` in
`render.py`) -- area and point-overlay extent-fitting are the same code path
now, not two.

**Known limitation:** geocoding a bare place name has no awareness of the
rest of the request, so an ambiguous name (there's a "Whittier" in both
Alaska and California) can resolve to the wrong one if nothing disambiguates
it. The system prompt asks the model to enrich an ambiguous reference with
inferable context from the rest of the request (state, region, other
references already resolved nearby) before it reaches the geocoder -- this
isn't foolproof, but it's the right layer to handle it: geocoding itself
stays a dumb, deterministic lookup.

Extent priority when no area is explicitly given: if the request has points
and/or lines, the chart auto-fits to their combined bounding box (with
padding) rather than falling back to the placeholder; otherwise it falls
back to a fixed placeholder region (Seattle, WA) at a sensible default scale
per chart type -- the phase-1 behavior, unchanged.

Point color defaults to red if the request doesn't name one; multiple
color/icon combinations in one request become separate labeled point layers.

Each point's location (`LLMLabeledPoint.location`) resolves through the same
reference mechanism as area corners and line waypoints
(`nlchart/geocode.py::resolve_point` -> `resolve_reference`) -- explicit
coordinates work exactly as before, and a named landmark or airport now
works too ("place an anchor on New York City", "mark Merrill Field with a
plane icon"), freely mixed with coordinate points in the same group. A
landmark point resolves to that place's geocoded center, not a
hand-verified exact spot -- fine for a general marker, but if a point needs
to land in a precise location, give it explicit coordinates instead of a
casual name.

Each point group can use one of QGIS's bundled recolorable icons instead of
a plain dot: `boat`, `helicopter`, `plane`, `car`, `flag`, `house`, `anchor`
(`_POINT_ICON_SVGS` in `render.py`; plain `dot` is the default). The model
picks an icon from an explicit request ("mark it with a helicopter") or a
clear contextual implication (a vessel's last known position -> boat, a
medevac LZ -> helicopter) -- but an explicit marker word in the request
("a blue **dot**") always wins over inference, since dot vs. icon is treated
the same as color: cosmetic, so honor what was literally asked for and only
infer when nothing was said.

### NL parsing

Free text is parsed by Claude (`nlchart/parsing/claude.py`, model
`claude-sonnet-5`) into a validated structured spec
(`nlchart/spec.py::LLMChartRequest`) -- chart type and, optionally, an area
reference. The model is only ever asked to *extract structure*; it never
generates or runs any PyQGIS code, and `render.py` is the sole thing that
touches QGIS. If a request is ambiguous, names an unsupported chart type, or
needs a feature not yet implemented (a coordinate table too malformed to
parse, importing a shapefile or other external GIS file, an AIS lookup),
the model is instructed to say so explicitly rather than guess.

Named places in an area reference (airports, place names) are resolved to
coordinates by a real geocoder (`nlchart/geocode.py`, OpenStreetMap
Nominatim) -- never by the model's memorized training data. A wrong guessed
coordinate is a real safety problem for a tool meant to hand emergency
responders a chart they can trust, so an unresolvable place name fails
loudly instead of rendering something plausible-looking but wrong.

**Requires `ANTHROPIC_API_KEY`** (or an `ant auth login` profile) in the
environment. `pip install --user anthropic` if not already installed.

## Basemap sources

All four are live public tile services, loaded via QGIS's `arcgismapserver`
raster provider (not a hand-rolled XYZ template) because most of them use
non-standard tile pyramids that only that provider resolves correctly:

| Type | Source | Notes |
|---|---|---|
| nautical | NOAA `MarineChart_Services/NOAACharts` | Coastal/harbor raster charts; sparse away from navigable water; renders 1:4.5k-1:295M |
| sectional | FAA `VFR_Sectional` | Only renders between 1:144k and 1:2.3M -- matches the real published chart scale |
| satellite | Esri `World_Imagery` | Full global coverage, standard Web Mercator pyramid, no scale restriction |
| topo | USGS National Map `USGSTopo` | Topographic quad style (contours, hillshade, trails); full standard 24-level global pyramid, no artificial scale restriction; for land/mountain search-and-rescue |

The FAA and NOAA scale bands, plus topo's floor, are hard-coded in
`render.py` (`_SCALE_BOUNDS`, currently nautical 1:20k-1:280M, sectional
1:175k-1:2.2M, topo 1:20k-1:280M) and any area-driven request whose implied
scale falls outside them is clamped to the nearest valid scale, with a
warning printed to stderr. These are padded well inside the services'
theoretical min/maxScale: testing found that QGIS's print-layout export for
these cached ArcGIS tile services can render completely blank for a scale
within roughly 5-8% of the max-detail edge -- a direct in-memory render at
the identical extent and resolution works fine, so this is specific to the
export path, not the tile data.

Export DPI (`_EXPORT_DPI_BY_TYPE` in `render.py`) is set **per chart type**,
not one global constant -- bisecting each type's own worst case (rendered
at its `_SCALE_BOUNDS` floor) found the blank-export bug is really about
how close a type's padded floor sits to its own tile service's real
max-detail edge, not a universal "high DPI near any zoom" problem:
nautical, satellite, and topo all render clean at the full 300 DPI even at
their tightest zoom (NOAA's floor has generous headroom past its ~1:4.5k
native edge, Esri World Imagery has no scale limit at all, and USGSTopo's
own real max-detail edge is far past its padded floor -- bisected down to a
blank cliff around 1:19k, well clear of the 1:20k floor), while sectional
still blanks out above roughly 170-180 DPI at its floor -- FAA's real
max-detail edge (~1:144k) sits too close to the padded floor (1:175k) for
that headroom to exist, so it stays capped at 150 DPI.

## Web frontend

A minimal web MVP sits in front of the same CLI pipeline -- no separate
code path, no separate parsing/rendering logic:

- **`webserver.py`** (repo root): a stdlib-only HTTP server (no new
  dependencies) wrapping `nlchart` directly. `POST /generate` checks a
  shared password (`hmac.compare_digest`, timing-safe), parses the request
  through the same `ClaudeParser`/`render_chart` pipeline the CLI uses, and
  streams the resulting PDF back. A single global lock serializes
  parse+render calls -- `QgsProject` is a process-wide singleton
  (`project.clear()`/`project.addMapLayer()` in `render.py`), so two
  renders running at once would corrupt each other's state.
- Runs as a persistent `systemd --user` service (`nlchart-web`, unit at
  `~/.config/systemd/user/nlchart-web.service`), restart-on-failure,
  survives reboot. Secrets (`ANTHROPIC_API_KEY`, the shared password) live
  in a chmod-600 env file, not in the unit itself.
- **Caddy** routes `/api/nlchart/*` on the existing homepage domain to the
  service (via the host's docker-gateway IP, since the backend runs on the
  host, not in a container) and strips the prefix; everything else still
  falls through to the static site's `file_server`.
- **Frontend**: a plain HTML page (`/data/homepage/public/maps/index.html`
  in the homepage's own repo, outside this one) -- a textarea for the NL
  request, a password field, and a submit button that POSTs JSON and
  triggers a browser download of the returned PDF blob, or shows the
  error text inline on a non-200 response.
- The password exists only to keep random internet bots from spending this
  box's Anthropic API budget on a public page -- it is not real user auth,
  and there's no rate limiting beyond that single shared secret.

## Tests

`tests/test_geo_math.py` is a real pytest suite (`pip install --user
pytest`) covering `nlchart/geo_math.py` -- the one module with no QGIS or
network dependency, so it's the one that can be tested in true isolation.
It locks in the same "verify against a known value before trusting it"
checks that were previously only ever run as throwaway scripts during
development: exact geometric identities (a meridian/equator is
simultaneously a great circle and a rhumb line, so both formulas must
agree; a destination point's distance back to its origin must round-trip
exactly), one real-world-grounded check (JFK-LAX great-circle distance
against the commonly published ~2475 SM figure, within the slack expected
from this module's spherical-Earth approximation vs. a real
ellipsoid), and area-unit conversions cross-checked against independent
unit definitions (acres from square feet, square miles/NM from the linear
mile/NM already in `UNIT_TO_METERS`) rather than round-tripped against
themselves. Run with `python3 -m pytest tests/`.

This does **not** cover `render.py`/QGIS-dependent code (polygon-area
measurement via `QgsDistanceArea`, layout construction, etc.) -- those are
still verified by direct `ChartSpec` construction + visual inspection of
the rendered PDF, as documented throughout this README, not by an
automated suite.

## Roadmap

1. ~~Recognize chart type, render basemap-only PDF~~
2. ~~Understand a geographic area in the request~~ -- LLM-based structured
   extraction (`nlchart/parsing/`) replaced the phase-1 keyword matcher; the
   schema (`nlchart/spec.py`) anticipates later phases as additive fields
3. ~~Overlay user-supplied labeled/colored points, route lines,
   vertex-defined polygons, and range rings~~ -- shapefile-imported
   polygons specifically are still not implemented and still refuse
   cleanly
4. ~~General print-layout polish: grid/graticule, title block conventions
   per chart type~~ -- a legend (color/icon key) was considered and
   deliberately skipped: every point/line/polygon/range-ring already
   carries an inline map label, so a separate key is lower value here than
   it would be on a typical unlabeled GIS map. Can revisit if real usage
   shows it's missed.
5. **Not planned: live AIS/ADSB vessel/aircraft position lookups.**
   Deliberately deprioritized rather than left as a future phase --
   `point_sets` already ingests a pasted coordinate table (see the example
   commands above), which is exactly the shape another tool's AIS/ADSB
   export already comes in, so the gap is already closed. A live
   integration would also add an ongoing-cost external dependency (API
   keys, rate limits) and a real safety problem specific to this tool's
   audience: a position is only accurate at fetch time, and baking
   "current position" into a static PDF invites someone to trust a stale
   dot. Shapefile-imported polygons are deferred for a related but
   different reason -- not a safety concern, just a larger, separate
   feature not yet scoped.

## Requirements

- System QGIS + its Python bindings (`python3-qgis`), reachable at whatever
  `python3` this is run with.
- `anthropic` and `requests` Python packages (`pip install --user anthropic`;
  `requests` ships with this environment already).
- `ANTHROPIC_API_KEY` set in the environment (or an `ant auth login`
  profile).
- `pytest` (`pip install --user pytest`) -- dev-only, for `tests/`.
