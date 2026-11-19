"""Loopback-only inspector for end-to-end website testing."""

import atexit
import json
import os
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .agent import Agent
from .questions import MAX_STEPS
from .test_suite import TestSuite
from .utils.config import PROJECT_ROOT, load_environment

ROOT = Path(__file__).parent
PORT = int(os.environ.get("TYPESAFE_DEMO_PORT", "8766"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
AGENT = None
SUITE = TestSuite()


def response_state():
    """Return the current serializable state plus inspector metadata."""
    state = AGENT.snapshot() if AGENT else {"page": None, "status": "idle", "history": [], "decision": None}
    return {
        **state,
        "decision_model": state.get("decision_model"),
        "text_model": os.environ.get("TEXT_MODEL", "deepseek-chat"),
        "max_steps": MAX_STEPS,
    }


def suite_state():
    """Return the current end-to-end suite state."""
    return SUITE.snapshot()


def close_browser():
    """Close and forget the active test agent."""
    global AGENT
    if AGENT:
        AGENT.close()
        AGENT = None


def start_url(value):
    """Accept only bounded HTTP or HTTPS starting URLs."""
    url = value.strip() if isinstance(value, str) else ""
    parsed = urlparse(url)
    if len(url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid http:// or https:// website URL")
    return url


def command(name, body):
    """Create a run or forward one bounded command to the active agent."""
    global AGENT
    if name == "reset":
        url = start_url(body.get("url", ""))
        provider = body.get("decision_provider", "")
        print(f"Starting test for {url}", flush=True)
        goal = body.get("goal", "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        close_browser()
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        logs = PROJECT_ROOT / "logs"
        save_screenshots = body.get("save_screenshots") is True
        AGENT = Agent(
            url,
            goal,
            decision_provider=provider,
            decision_log=logs / f"jev-decisions-{run_id}.log",
            screenshots=True,
            model_screenshots=body.get("model_screenshots") is True,
            record_dir=logs / f"jev-screenshots-{run_id}" if save_screenshots else None,
        )
    else:
        if AGENT is None:
            raise ValueError("Start a test first")
        AGENT.command(name, body)
    state = response_state()
    SUITE.finish(state)
    return state


def suite_command(name, body):
    """Load a suite, start one test, or record an execution error."""
    if name == "suite/load":
        url = start_url(body.get("url", ""))
        provider = body.get("decision_provider", "")
        if provider not in {"typesafe", "openrouter"}:
            raise ValueError("Choose a decision model")
        options = {
            "decision_provider": provider,
            "model_screenshots": False,
            "save_screenshots": False,
        }
        state = SUITE.load(url, body.get("suite"), options)
        close_browser()
        return state

    if name == "suite/clear":
        close_browser()
        SUITE.clear()
        return suite_state()

    if name == "suite/start":
        test = SUITE.start(body.get("test_id"))
        return command(
            "reset",
            {
                "url": SUITE.url,
                "goal": test["description"],
                **SUITE.options,
            },
        )

    if name == "suite/fail":
        SUITE.fail(body.get("test_id"), body.get("error", "Test execution failed"))
        return suite_state()

    return command(name, body)


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        """Send one no-cache response, tolerating clients that already disconnected."""
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        # Accept requests only for the expected local server.
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")

        # Get the requested path, excluding any query parameters (?x=...).
        path = urlparse(self.path).path

        # List the interface files this server can serve.
        files = {
            "/": ("home.html", "text/html"),
            "/classic": ("index.html", "text/html"),
            "/suite": ("suite.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/suite.js": ("suite.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
        }

        # Check whether the path is supported.
        if path == "/api/state":
            with LOCK:
                return self.send(200, json.dumps(response_state()))
        if path == "/api/suite":
            with LOCK:
                return self.send(200, json.dumps(suite_state()))
        if path not in files:
            return self.send(404, "Not found", "text/plain")

        # Read the requested file and insert the token used by the interface.
        name, mime = files[path]
        content = (ROOT / "static" / name).read_text().replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def do_POST(self):
        # Check that this request comes from the local interface.
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local demo requests only"}))

        # Allow only one browser command to run at a time.
        if not LOCK.acquire(blocking=False):
            return self.send(409, json.dumps({"error": "A browser step is already running"}))

        try:
            # Check the request size, then parse its JSON body.
            body_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < body_length < 262144:
                raise ValueError("Invalid request size")

            body = json.loads(self.rfile.read(body_length))
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")

            # Extract the command and execute it.
            command_name = urlparse(self.path).path.removeprefix("/api/")
            result = suite_command(command_name, body)
            self.send(200, json.dumps(result))

        # Return expected errors to the interface; hide unexpected details.
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            self.send(500, json.dumps({"error": "Local test failed; no automatic retry. Reset to recover."}))

        # Always release the lock, including when command handling fails.
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def main():
    load_environment()
    atexit.register(close_browser)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev Ultrafast tester: {ORIGIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
