"""Minimal HTTP server exposing nlchart to the web: POST /generate -> PDF.

Stdlib only, deliberately -- this is an MVP wrapper around the existing
nlchart library (parsing/claude.py, geocode.py, render.py), not a new
architecture. A single global lock serializes parse+render calls: QGIS's
QgsProject is a process-wide singleton (see render.py's
project.clear()/project.addMapLayer() calls), so two renders running at
once would corrupt each other's state.

Auth is a single shared password compared with hmac.compare_digest (timing
safe), read from NLCHART_WEB_PASSWORD. This is not real user auth -- it
exists only to keep random internet bots from spending this box's
Anthropic API budget on a public page.
"""

import hmac
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nlchart.geocode import GeocodeError
from nlchart.parsing.base import ParseError
from nlchart.parsing.claude import ClaudeParser
from nlchart.render import ChartRenderError, render_chart

_PASSWORD = os.environ["NLCHART_WEB_PASSWORD"]
_PORT = int(os.environ.get("NLCHART_WEB_PORT", "8877"))
_MAX_BODY_BYTES = 20_000

_render_lock = threading.Lock()
_parser = ClaudeParser()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/generate":
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._send_json(400, {"error": "request body missing or too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "malformed JSON body"})
            return

        password = payload.get("password", "")
        if not hmac.compare_digest(str(password), _PASSWORD):
            self._send_json(403, {"error": "incorrect password"})
            return

        text = (payload.get("text") or "").strip()
        if not text:
            self._send_json(400, {"error": "enter a request describing the chart you want"})
            return

        output_path = None
        try:
            with _render_lock:
                chart_spec = _parser.parse(text)
                fd, output_path = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                render_chart(chart_spec, output_path)

            with open(output_path, "rb") as f:
                data = f.read()
        except (ParseError, GeocodeError, ChartRenderError) as exc:
            self._send_json(422, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 -- last-resort guard for an unattended public endpoint
            print(f"unexpected error: {exc!r}", file=sys.stderr)
            self._send_json(500, {"error": "unexpected server error"})
            return
        finally:
            if output_path is not None and os.path.exists(output_path):
                os.remove(output_path)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{chart_spec.chart_type}.pdf"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", _PORT), Handler)
    print(f"nlchart web server listening on 0.0.0.0:{_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
