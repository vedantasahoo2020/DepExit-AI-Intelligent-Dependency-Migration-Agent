"""Local web dashboard: `depexit web` (or double-click start-depexit) opens http://127.0.0.1:8000

Runs entirely on your machine (no cloud hosting). One analysis at a time; every run is kept in
depexit-runs/ and can be reopened from the dashboard.
"""
import json
import mimetypes
import os
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from rich.console import Console

STATIC = Path(__file__).parent / "static"
RUNS = Path("depexit-runs").resolve()
ENV_FILE = Path(".env").resolve()


class _LogWriter:
    def __init__(self, state):
        self.state = state
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.state["log"].append(line.rstrip())
        return len(text)

    def flush(self):
        pass


STATE = {"running": False, "done": False, "error": None, "log": [], "out": None, "started": None,
         "finished": None, "params": None}
LOCK = threading.Lock()


def _has_key():
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def _run(params):
    from . import pipeline

    try:
        pipeline.set_console(Console(file=_LogWriter(STATE), width=110, color_system=None, force_terminal=False))
        out = RUNS / datetime.now().strftime("%Y%m%d-%H%M%S") / "capsule"
        STATE["out"] = str(out)
        forecast = [v.strip() for v in (params.get("forecast") or "").split(",") if v.strip()]
        only = [v.strip() for v in (params.get("only") or "").split(",") if v.strip()]
        pipeline.analyze(params["target"].strip(), out_dir=out, python=params.get("python") or "3.12",
                         use_ai=bool(params.get("use_ai")), only=only or None,
                         attempts=int(params.get("attempts") or 2), max_deps=int(params.get("max_deps") or 8),
                         work=".depexit-work", model=(params.get("model") or "").strip() or None,
                         forecast=forecast or None, organize=bool(params.get("organize", True)))
    except SystemExit as exc:
        STATE["error"] = str(exc)
    except Exception as exc:  # shown in the UI instead of killing the server
        msg = str(exc)
        if "GEMINI_API_KEY" in msg:
            msg = "Gemma needs an API key. Add it in the Gemma key field, or switch Gemma off."
        STATE["error"] = msg or type(exc).__name__
        STATE["log"].extend(traceback.format_exc().splitlines()[-8:])
    finally:
        STATE["running"] = False
        STATE["done"] = True
        STATE["finished"] = time.time()


def _load_result(out):
    out = Path(out or "")
    index = out / "capsule.json"
    if not index.exists():
        return None
    data = json.loads(index.read_text(encoding="utf-8"))
    data["capsule_docs"] = {}
    for rel in data.get("capsules", []):
        p = out / rel
        if p.exists():
            cap = json.loads(p.read_text(encoding="utf-8"))
            data["capsule_docs"][cap["dependency"]["name"]] = cap
    data["files"] = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    data["run_id"] = out.parent.name
    return data


def _runs():
    items = []
    if not RUNS.exists():
        return items
    for d in sorted(RUNS.iterdir(), reverse=True):
        idx = d / "capsule" / "capsule.json"
        if not idx.exists():
            continue
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
        except ValueError:
            continue
        deps = data.get("all_dependencies", [])
        items.append({
            "id": d.name,
            "target": data.get("run", {}).get("target"),
            "finished": data.get("run", {}).get("finished"),
            "ai_model": data.get("run", {}).get("ai_model"),
            "scanned": len(deps),
            "verified": sum(1 for x in deps if (x.get("verdict") or "").startswith("verified")),
        })
    return items[:30]


def _save_key(key):
    """Store the key in tool/.env (git-ignored) so it survives restarts."""
    lines = []
    if ENV_FILE.exists():
        lines = [ln for ln in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if not ln.strip().startswith(("GEMINI_API_KEY", "GOOGLE_API_KEY"))]
    lines.append("GEMINI_API_KEY=%s" % key)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)) or body is None:
            body = json.dumps(body, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            return self._send(200, {
                "running": STATE["running"], "done": STATE["done"], "error": STATE["error"],
                "log": STATE["log"][since:], "n": len(STATE["log"]), "out": STATE["out"],
                "started": STATE["started"], "finished": STATE["finished"], "now": time.time(),
                "params": STATE["params"], "has_key": _has_key(),
                "model": os.environ.get("GEMMA_MODEL", "gemma-4-26b-a4b-it")})
        if url.path == "/api/result":
            return self._send(200, _load_result(STATE["out"]))
        if url.path == "/api/runs":
            return self._send(200, _runs())
        if url.path.startswith("/files/") and STATE["out"]:
            base = Path(STATE["out"]).resolve()
            target = (base / unquote(url.path[len("/files/"):])).resolve()
            if base in target.parents and target.is_file():
                ctype = mimetypes.guess_type(target.name)[0] or "text/plain"
                if target.suffix in (".md", ".patch"):
                    ctype = "text/plain"
                return self._send(200, target.read_bytes(), ctype + "; charset=utf-8")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/run":
            params = self._body()
            if not (params.get("target") or "").strip():
                return self._send(400, {"error": "Enter a git URL or a folder path to analyse."})
            if params.get("use_ai") and not _has_key():
                return self._send(400, {"error": "Gemma needs an API key. Add it under Gemma key, or switch Gemma off."})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "An analysis is already running."})
                STATE.update(running=True, done=False, error=None, log=[], out=None, finished=None,
                             started=time.time(), params=params)
            threading.Thread(target=_run, args=(params,), daemon=True).start()
            return self._send(200, {"ok": True})
        if url.path == "/api/key":
            p = self._body()
            key = (p.get("key") or "").strip()
            if not key:
                return self._send(400, {"error": "Paste a key first."})
            os.environ["GEMINI_API_KEY"] = key
            if p.get("remember"):
                _save_key(key)
            return self._send(200, {"ok": True, "saved": bool(p.get("remember"))})
        if url.path == "/api/load":
            p = self._body()
            if STATE["running"]:
                return self._send(409, {"error": "Wait for the current analysis to finish."})
            out = RUNS / str(p.get("id", "")) / "capsule"
            if not (out / "capsule.json").exists() or RUNS not in out.resolve().parents:
                return self._send(404, {"error": "Run not found."})
            STATE.update(out=str(out), done=True, error=None, log=[], started=None, finished=None)
            return self._send(200, _load_result(out))
        if url.path == "/api/branch":
            from .gitbranch import create_branch

            p = self._body()
            if not STATE["out"]:
                return self._send(400, {"ok": False, "error": "Run an analysis first."})
            which = ["migration"] + (["organizer"] if p.get("organizer", True) else [])
            try:
                res = create_branch(p.get("repo", ""), STATE["out"], which, p.get("name") or None)
            except Exception as exc:
                res = {"ok": False, "error": str(exc), "steps": []}
            return self._send(200, res)
        return self._send(404, {"error": "not found"})


def serve(host="127.0.0.1", port=8000, open_browser=True):
    RUNS.mkdir(parents=True, exist_ok=True)
    server = None
    for p in range(port, port + 10):  # if 8000 is busy, use the next free port
        try:
            server = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("No free port between %d and %d" % (port, port + 9))
    url = "http://%s:%d" % (host, port)
    print("DepExit dashboard: %s  (keep this window open; Ctrl+C to stop)" % url)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
