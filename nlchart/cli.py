import argparse
import sys

from .geocode import GeocodeError
from .parsing.base import ParseError
from .parsing.claude import ClaudeParser
from .render import render_chart, ChartRenderError


def main(argv=None) -> int:
    arg_parser = argparse.ArgumentParser(
        prog="nlchart",
        description="Turn a natural-language chart request into a printable PDF chart.",
    )
    arg_parser.add_argument(
        "request", help='e.g. "get me a sectional chart of the area 50 SM around Anchorage airport"'
    )
    arg_parser.add_argument("-o", "--output", default=None, help="Output PDF path")
    args = arg_parser.parse_args(argv)

    parser = ClaudeParser()
    try:
        spec = parser.parse(args.request)
    except (ParseError, GeocodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output_path = args.output or f"output/{spec.chart_type}.pdf"

    try:
        render_chart(spec, output_path)
    except ChartRenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
