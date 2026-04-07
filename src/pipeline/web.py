"""Loopback-only operator HTTP server with same-origin, session and CSRF guards."""

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit, parse_qs
from .web_api import WebAPI

STATIC = Path(__file__).parent / "static"
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


class Server(ThreadingHTTPServer):
    daemon_threads = True


def create_server(state, port=8765):
    api = WebAPI(state)
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            # OAuth codes, API payloads and correspondence never go to HTTP access logs.
            pass

        def send_data(self, status, data, content_type="application/json", headers=None, csp=CSP):
            raw = (
                json.dumps(data, default=str).encode()
                if content_type == "application/json"
                else data.encode()
                if isinstance(data, str)
                else data
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Connection", "close")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def host_ok(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def session_ok(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                supplied = cookie.get("pipeline_session")
                return bool(supplied and secrets.compare_digest(supplied.value, session))
            except Exception:
                return False

        def origin(self):
            return "http://" + self.headers["Host"]

        def guard(self, mutation=False):
            if not self.host_ok():
                self.send_data(403, {"error": "Only the local Pipeline address is allowed"})
                return False
            origin = self.headers.get("Origin")
            if (origin and origin != self.origin()) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_data(403, {"error": "Cross-origin access is not allowed"})
                return False
            if not self.session_ok() or (
                mutation and not secrets.compare_digest(self.headers.get("X-Pipeline-CSRF", ""), csrf)
            ):
                self.send_data(403, {"error": "Reload Pipeline to refresh your local session"})
                return False
            return True

        def do_GET(self):
            if not self.host_ok():
                return self.send_data(403, {"error": "Invalid local host"})
            path = urlsplit(self.path).path
            if path == "/api/session":
                if (
                    self.headers.get("Origin") not in {None, self.origin()}
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                ):
                    return self.send_data(403, {"error": "Cross-origin access is not allowed"})
                return self.send_data(
                    200,
                    {"csrf": csrf},
                    headers={"Set-Cookie": f"pipeline_session={session}; HttpOnly; SameSite=Lax; Path=/"},
                )
            if path == "/oauth/callback":
                if not self.session_ok():
                    return self.send_data(400, {"error": "Open setup again to connect Google"})
                try:
                    api.oauth_callback(parse_qs(urlsplit(self.path).query), session)
                    return self.send_data(303, b"", "text/plain", {"Location": "/#setup"})
                except Exception as exc:
                    error = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "Google authorization failed. Return to setup and try again."
                    )
                    return self.send_data(400, {"error": error})
            if path.startswith("/google-research/"):
                if not self.guard():
                    return
                from .google_research import VIEWER_CSP

                page = api.research_view(path.rsplit("/", 1)[-1])
                if page is None:
                    return self.send_data(
                        404, {"error": "Research view expired. Run a new question in Pipeline."}
                    )
                return self.send_data(200, page, "text/html; charset=utf-8", csp=VIEWER_CSP)
            if path == "/api/snapshot":
                if self.guard():
                    try:
                        return self.send_data(200, api.snapshot())
                    except Exception:
                        return self.send_data(
                            500, {"error": "Cannot read app state; inspect server configuration"}
                        )
                return
            if path.startswith("/api/contact/"):
                if not self.guard():
                    return
                try:
                    id = int(path.rsplit("/", 1)[-1])
                    row = api.store.one("select * from contacts where id=?", (id,))
                    if not row:
                        return self.send_data(404, {"error": "Unknown contact"})
                    row["research"] = json.loads(row.pop("seed"))
                    return self.send_data(200, row)
                except ValueError:
                    return self.send_data(400, {"error": "Invalid contact ID"})
            if path == "/favicon.ico":
                return self.send_data(204, b"", "image/x-icon")
            files = {
                "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            }
            if path not in files:
                return self.send_data(404, {"error": "Not found"})
            name, type = files[path]
            return self.send_data(200, (STATIC / name).read_bytes(), type)

        def do_POST(self):
            if not self.guard(mutation=True):
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send_data(415, {"error": "Use JSON"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_000_000:
                    return self.send_data(413, {"error": "Request must be 1 byte to 1 MB"})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("JSON object required")
                result = api.post(urlsplit(self.path).path, data, self.origin(), session)
                self.send_data(200, result)
            except (ValueError, KeyError, FileNotFoundError, RuntimeError) as exc:
                # Validation errors are sanitized so uploaded secrets never appear in Pydantic traces.
                from pydantic import ValidationError

                if isinstance(exc, ValidationError):
                    message = "; ".join(
                        ".".join(map(str, e["loc"])) + ": " + e["msg"]
                        for e in exc.errors(include_input=False)
                    )
                elif isinstance(exc, KeyError):
                    message = "Missing field: " + str(exc)
                else:
                    message = str(exc)
                self.send_data(400, {"error": message})
            except Exception as exc:
                self.send_data(
                    400, {"error": type(exc).__name__ + ". Check connection settings and try again."}
                )

    httpd = Server(("127.0.0.1", port), Handler)
    httpd.api = api
    return httpd


def serve(state="state", port=8765, open_browser=False):
    server = create_server(state, port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Pipeline is available at {url}", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    import signal

    def terminate(signum, frame):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        server.server_close()
        if server.api.worker.status()["owned"]:
            server.api.worker.stop()
