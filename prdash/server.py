"""Local HTTP server: the page, a JSON API, and a server-sent-events stream for live updates."""
import json, queue, secrets, shutil, time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config, db, github, integrations, local, sync

WEB = Path(__file__).resolve().parent / "web"
TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".html": "text/html"}
PORT = 8765
DEMO = False

# ops that act on one PR. Each takes the cached PR dict and the optional body text.
OPS = {
    **{op: (lambda op: lambda pr, body: github.act(op, pr["url"], body))(op) for op in github.ACTIONS},
    "rerun": lambda pr, body: github.rerun_failed(pr["url"]),
    "claude": lambda pr, body: local.claude(pr),
    "review": lambda pr, body: (local.start_review(pr, _review_done), "Claude review started")[1],
    "editor": lambda pr, body: local.editor(pr),
    "terminal": lambda pr, body: local.terminal(pr),
    "checkout": lambda pr, body: local.checkout(pr),
    "worktree": lambda pr, body: "worktree at " + local.worktree(pr),
    "worktree_remove": lambda pr, body: local.worktree_remove(pr),
    "clone": lambda pr, body: local.clone(pr),
    "slack_post": lambda pr, body: integrations.slack_post(pr),
    "slack_find": lambda pr, body: "found it" if integrations.slack_find(pr) else "not posted yet",
}
CHANGES_GITHUB = set(github.ACTIONS) | {"rerun"}


def _review_done(pr):
    sync.broadcast({"type": "review", "url": pr["url"]})
    status = db.reviews().get(pr["url"], {}).get("status")
    db.add_events([(pr["url"], "review_done" if status == "done" else "review_error",
                    f'Claude review {"ready" if status == "done" else "failed"}: {sync.label(pr)}')])
    sync.broadcast({"type": "inbox"})


def token():
    t = db.get("token")
    if not t:
        legacy = db.HOME / "token"  # pre-SQLite installs kept it in a file
        t = legacy.read_text().strip() if legacy.exists() else secrets.token_urlsafe(24)
        db.put("token", t)
    return t


def login_url(port):
    """A one-time link that logs a browser in. It can show up briefly in process lists when a browser
    is launched, so it expires after two minutes and works once; the session lives in an HttpOnly cookie."""
    key = secrets.token_urlsafe(24)
    with db.LOCK:
        keys = {k: exp for k, exp in db.get("login_keys", {}).items() if exp > time.time()}
        keys[key] = time.time() + 120
        db.put("login_keys", keys)
    return f"http://127.0.0.1:{port}/?key={key}"


def use_login_key(key):
    with db.LOCK:
        keys = db.get("login_keys", {})
        ok = keys.pop(key, 0) > time.time()
        db.put("login_keys", keys)
    return ok


LOCKED = b"""<!doctype html><meta charset="utf-8"><title>prdash</title>
<body style="font:15px system-ui;background:#0d0f12;color:#dde1e8;display:grid;place-items:center;height:90vh;text-align:center">
<div><h2>prdash is running</h2><p>Open it with the <code>prdash</code> command; it logs this browser in.<br>
Or run <code>prdash url</code> and paste the link it prints.</p></div>"""


def pr_for(url):
    if not github.allowed(url):
        raise ValueError("Not a PR in your configured orgs.")
    pr = db.get_prs([url]).get(url)
    if not pr:
        raise ValueError("PR isn't in the local cache yet; wait for the next sync.")
    return pr


def run_op(url, op, body=""):
    if DEMO:
        raise ValueError("This is the demo: actions are turned off. Run prdash without --demo to use your own PRs.")
    if op not in OPS:
        raise ValueError(f"unknown action {op}")
    out = OPS[op](pr_for(url), body or "")
    if op in CHANGES_GITHUB:
        sync.now()
    return out


def detail(url):
    """Slower per-PR info the drawer loads lazily, fetched in parallel; each part fails on its own."""
    pr = pr_for(url)
    if DEMO:
        from . import demo
        return demo.detail(pr)
    parts = {"checks": lambda: github.checks(url), "git": lambda: local.state(pr)}
    if integrations.slack_ready():
        parts["slack"] = lambda: integrations.slack_thread(pr)
    if integrations.jira_ready() and pr["jira"]:
        parts["jira"] = lambda: integrations.jira_issues(set(pr["jira"]))
    with ThreadPoolExecutor(len(parts)) as ex:
        futures = {k: ex.submit(f) for k, f in parts.items()}
    out = {}
    for k, f in futures.items():
        try:
            out[k] = f.result()
        except Exception as e:  # show the error in that section, keep the rest
            out[k + "_error"] = str(e)
    return out


def test_integration(what):
    if what == "slack":
        who = integrations.slack("auth.test")
        return f'Connected to {who.get("team")} as {who.get("user")}'
    if what == "jira":
        me = integrations.jira("/rest/api/3/myself")
        return f'Connected as {me.get("displayName")}'
    if what == "notify":
        sync.notify([(None, "test", "prdash test notification")])
        return "Sent (if a notify command is set)"
    raise ValueError(what)


def setup_info():
    try:
        orgs = github.gh("api", "user/orgs", "--paginate", "--jq", ".[].login").split()
    except RuntimeError:
        orgs = []
    claude = config.expand(config.get("claude_cmd"))
    return {"me": github.viewer(), "orgs": orgs, "selected": github.owners(), "dirs": local.find_clone_dirs(),
            "repo_dirs": config.get("repo_dirs"), "terminal": config.get("terminal"), "editor": config.get("editor"),
            "claude": bool(claude and shutil.which(claude[0]))}


def setup_save(b):
    config.put("orgs", ",".join(b.get("orgs") or []))
    for key in ("repo_dirs", "terminal", "editor"):
        if b.get(key):
            config.put(key, b[key])
    db.put("orgs_auto", None)
    db.put("setup_done", True)
    sync.now()


def settings_data():
    return {"fields": config.public(), "slack": integrations.slack_ready(), "jira": integrations.jira_ready(),
            "owners": github.owners(), "me": github.viewer()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400" if ctype in ("font/woff2", "image/svg+xml") else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        """Session cookie (browsers) or X-Token header (scripts). The token itself is never served to a page:
        other local users or processes can reach 127.0.0.1 but can't read the cookie or prdash's 0700 data dir."""
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        given = self.headers.get("X-Token") or (jar["prdash"].value if "prdash" in jar else "")
        return bool(given) and secrets.compare_digest(given, token())

    def trusted(self, path, qs):
        # Host check blocks DNS rebinding; the session (SameSite=Strict) blocks other sites from calling the API
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        return self.authed() if path.startswith("/api/") else True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        u = urlsplit(self.path)
        path, qs = u.path, parse_qs(u.query)
        if not self.trusted(path, qs):
            return self.send(403, {"error": "forbidden"})
        try:
            if method == "GET":
                if path == "/":
                    if "key" in qs:  # one-time login link: swap it for the session cookie, then drop it from the URL
                        if not use_login_key(qs["key"][0]):
                            return self.send(401, LOCKED, "text/html; charset=utf-8")
                        self.send_response(303)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", f"prdash={token()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
                        self.send_header("Content-Length", "0")
                        return self.end_headers()
                    if not self.authed():
                        return self.send(401, LOCKED, "text/html; charset=utf-8")
                    return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    f = (WEB / path[8:]).resolve()
                    if f.is_relative_to(WEB) and f.is_file() and f.suffix in TYPES:
                        return self.send(200, f.read_bytes(), TYPES[f.suffix])
                    return self.send(404, {"error": "not found"})
                if path == "/api/state":
                    return self.send(200, {**sync.snapshot(), "config": self.page_config()})
                if path == "/api/stream":
                    return self.stream()
                if path == "/api/detail":
                    return self.send(200, detail(qs["url"][0]))
                if path == "/api/events":
                    return self.send(200, {"events": db.events()})
                if path == "/api/settings":
                    return self.send(200, settings_data())
                if path == "/api/setup":
                    return self.send(200, setup_info())
            if method == "POST":
                b = self.body()
                if path == "/api/sync":
                    sync.now()
                    return self.send(200, {"ok": True})
                if path == "/api/local":
                    pr_for(b.get("url", ""))
                    if not isinstance(b.get("fields"), dict):
                        raise ValueError("fields must be an object")
                    return self.send(200, {"local": db.local_update(b["url"], b["fields"])})
                if path == "/api/action":
                    return self.send(200, {"out": run_op(b.get("url", ""), b.get("op"), b.get("body"))})
                if path == "/api/batch":
                    return self.send(200, {"results": self.batch(b)})
                if path == "/api/events/read":
                    db.mark_read(b.get("ids"))
                    return self.send(200, {"ok": True})
                if path == "/api/settings":
                    for k, v in b.items():
                        config.put(k, v)
                    if "orgs" in b:
                        db.put("orgs_auto", None)
                    sync.now()
                    return self.send(200, settings_data())
                if path == "/api/setup":
                    setup_save(b)
                    return self.send(200, {"ok": True})
                if path == "/api/settings/test":
                    return self.send(200, {"out": test_integration(b.get("what"))})
            self.send(404, {"error": "not found"})
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            self.send(400, {"error": str(e) or type(e).__name__})

    def batch(self, b):
        results = []
        for url in b.get("urls", [])[:200]:
            try:
                if b.get("op") == "local":
                    pr_for(url)
                    db.local_update(url, b.get("fields") or {})
                    out = "ok"
                else:
                    out = run_op(url, b.get("op"), b.get("body"))
                results.append({"url": url, "ok": True, "out": out})
            except Exception as e:
                results.append({"url": url, "ok": False, "out": str(e)})
        return results

    def page_config(self):
        return {"demo": DEMO, "setup_done": bool(db.get("setup_done")), "jira_url": config.get("jira_url").rstrip("/"), "slack": integrations.slack_ready(),
                "slack_channel": config.get("slack_channel"), "jira": integrations.jira_ready(),
                "merge_method": config.get("merge_method"), "stale_days": config.get("stale_days"),
                "merged_days": config.get("merged_days"), "team_view": config.get("team_view"),
                "notify_browser": config.get("notify_browser"), "slack_template": config.get("slack_template")}

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = sync.subscribe()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            while True:
                try:
                    msg = f"data: {json.dumps(q.get(timeout=20))}\n\n"
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            sync.unsubscribe(q)
            self.close_connection = True

    def log_message(self, *a):
        pass


def serve(port):
    global PORT
    PORT = port
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()
