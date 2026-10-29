"""Local HTTP server: the page, a JSON API, and a server-sent-events stream for live updates."""
import hashlib, hmac, json, queue, re, secrets, shutil, statistics, time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import ai, config, db, github, integrations, local, standup, sync

WEB = Path(__file__).resolve().parent / "web"
TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2"}
PORT = 8765
DEMO = False

# ops that act on one PR. Each takes the cached PR dict and the optional body text.
OPS = {
    **{op: (lambda op: lambda pr, body, arg: github.act(op, pr["url"], body))(op) for op in github.ACTIONS},
    "rerequest": lambda pr, body, arg: github.rerequest(pr, [l.strip() for l in (arg or "").split(",") if l.strip()]),
    "request": lambda pr, body, arg: request_review(pr, arg if isinstance(arg, dict) else {}),
    "thread_reply": lambda pr, body, arg: github.thread_reply(pr["url"], arg or "", body),
    "thread_resolve": lambda pr, body, arg: github.thread_resolve(pr["url"], arg or "", True),
    "thread_unresolve": lambda pr, body, arg: github.thread_resolve(pr["url"], arg or "", False),
    "fix": lambda pr, body, arg: local.fix(pr, github.conversation(pr["url"])),
    "retry": lambda pr, body, arg: start_retry(pr),
    "retry_stop": lambda pr, body, arg: stop_retry(pr),
    "rerun": lambda pr, body, arg: github.rerun_failed(pr["url"]),
    "agent": lambda pr, body, arg: local.agent(pr),
    "review": lambda pr, body, arg: start_review(pr, arg == "since"),
    "editor": lambda pr, body, arg: local.editor(pr),
    "terminal": lambda pr, body, arg: local.terminal(pr),
    "checkout": lambda pr, body, arg: local.checkout(pr),
    "worktree": lambda pr, body, arg: "worktree at " + local.worktree(pr),
    "worktree_remove": lambda pr, body, arg: local.worktree_remove(pr),
    "clone": lambda pr, body, arg: local.clone(pr),
    "slack_post": lambda pr, body, arg: integrations.slack_post(pr),
    "slack_find": lambda pr, body, arg: "found it" if integrations.slack_find(pr) else "not posted yet",
}
CHANGES_GITHUB = set(github.ACTIONS) | {"rerun", "rerequest", "request", "thread_reply", "thread_resolve", "thread_unresolve", "retry"}


def start_review(pr, since=False):
    """since: only what changed after your last review of it, with your earlier comments."""
    commit = next((r.get("commit") for r in pr["reviewers"] if r["who"] == github.viewer()), None) if since else None
    if since and commit in (None, pr["head"]):
        raise ValueError("Nothing new since your last review.")
    local.start_review(pr, _review_done, commit)
    return "Claude review started"


def request_review(pr, arg):
    """Request review now, or arm it for when CI is green (sync.requests_when_green). The choice is remembered per owner."""
    names = lambda k: [x for x in (arg.get(k) if isinstance(arg.get(k), list) else []) if isinstance(x, str) and re.fullmatch(r"[\w.-]+", x)]
    r = {"teams": names("teams"), "users": names("users"), "slack": bool(arg.get("slack"))}
    if not r["teams"] and not r["users"]:
        raise ValueError("Pick someone to review.")
    if pr["merged"] or pr["state"] != "OPEN":
        raise ValueError("This PR isn't open.")
    with db.LOCK:
        db.put("request_prefs", {**db.get("request_prefs", {}), pr["owner"]: r})
    if not arg.get("green"):
        return sync.request_now(pr, r)
    # armed on a red PR: wait for a fix to be pushed, not for the current failure to clear
    db.local_update(pr["url"], {"request_when_green": {**r, "at": time.time(), "skip": pr["head"] if pr["ci"] in ("FAILURE", "ERROR") else None}})
    return "will request review once CI is green"


def request_options(pr):
    return {**github.candidates(pr), "prefs": db.get("request_prefs", {})}


def _review_done(pr):
    sync.broadcast({"type": "review", "url": pr["url"]})
    status = db.reviews().get(pr["url"], {}).get("status")
    db.add_events([(pr["url"], "review_done" if status == "done" else "review_error",
                    f'{ai.name()} review {"ready" if status == "done" else "failed"}: {sync.label(pr)}')])
    sync.broadcast({"type": "inbox"})


def token():
    """Master API token for scripts (X-Token). Never sent to a browser. Rotated once (auth v2) because
    earlier versions put it in a page and then in a cookie, so any copy of the old one stops working."""
    with db.LOCK:
        t = db.get("token")
        if not t or db.get("auth_version", 1) < 2:
            t = secrets.token_urlsafe(32)
            db.put("token", t)
            db.put("sessions", {})
            db.put("auth_version", 2)
            (db.HOME / "token").unlink(missing_ok=True)  # pre-SQLite installs kept it in a file
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


SESSION_DAYS = 7


def new_session():
    """Browser sessions are their own random IDs, never the master token, so a cookie that leaks to
    another localhost port (browsers don't isolate cookies by port) can be revoked and expires."""
    sid = secrets.token_urlsafe(24)
    with db.LOCK:
        sessions = {s: exp for s, exp in db.get("sessions", {}).items() if exp > time.time()}
        sessions[sid] = time.time() + SESSION_DAYS * 86400
        db.put("sessions", sessions)
    return sid


def valid_session(sid):
    exp = db.get("sessions", {}).get(sid, 0)
    return exp > time.time()


def csrf(sid):
    """Per-session token that only prdash's own page receives (a meta tag in index.html). Other localhost
    ports get the cookie but can't read this origin's page, so they can't produce it."""
    return hmac.new(token().encode(), sid.encode(), hashlib.sha256).hexdigest()


def logout_all():
    db.put("sessions", {})


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


def start_retry(pr):
    """Arm retry until green and re-run the failed jobs now; the sync loop takes it from there."""
    head, cs = github.checks_at(pr["url"])
    failed = github.failed_runs(cs)
    r = {"head": head, "attempts": 0, "max": max(1, min(10, config.get("retry_max")))}
    if failed:
        github.rerun_runs(pr["url"], failed)
        r.update(attempts=1, at=time.time())
    db.local_update(pr["url"], {"retry": r, "retry_done": None})
    return f'retrying until green, up to {r["max"]} re-runs' if failed else "will re-run failed jobs until green"


def stop_retry(pr):
    db.local_update(pr["url"], {"retry": None, "retry_done": pr.get("head")})
    return "stopped retrying"


def run_op(url, op, body="", arg=None):
    if DEMO:
        raise ValueError("This is the demo: actions are turned off. Run prdash without --demo to use your own PRs.")
    if op not in OPS:
        raise ValueError(f"unknown action {op}")
    out = OPS[op](pr_for(url), body or "", arg)
    if op in CHANGES_GITHUB:
        sync.now()
    return out


def detail(url):
    """Slower per-PR info the drawer loads lazily, fetched in parallel; each part fails on its own."""
    pr = pr_for(url)
    if DEMO:
        from . import demo
        return demo.detail(pr)
    parts = {"checks": lambda: github.checks(url), "conversation": lambda: github.conversation(url), "git": lambda: local.state(pr)}
    if not pr["merged"] and pr["state"] == "OPEN":
        parts["request"] = lambda: request_options(pr)
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
    counts = db.flake_counts()
    for c in out.get("checks", []):
        c["flakes"] = counts.get((f'{pr["owner"]}/{pr["repo"]}', c["name"]), 0)
    return out


def insights(view):
    """Turnaround for a view's PRs (merged ones in the window, plus what's open) and the flakiest checks."""
    prs, views = db.all_prs()
    ps = [prs[u] for u in views.get(view, []) if u in prs]
    hours = lambda a, b: (datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 3600
    med = lambda xs: round(statistics.median(xs), 1) if xs else None
    merged = [p for p in ps if p["merged"]]
    now = datetime.now(timezone.utc).isoformat()
    waiting = sorted(([p, hours(p["created"], now)] for p in ps if not p["merged"] and not p["draft"] and not p.get("first_review")
                      and p["state"] == "OPEN"), key=lambda x: -x[1])
    counts = db.flake_counts()
    by_check = {}
    for f in db.flakes():
        e = by_check.setdefault((f["repo"], f["name"]), {"repo": f["repo"], "name": f["name"], "count": 0, "last": f["ts"], "prs": []})
        e["count"] += 1
        if f["url"] not in e["prs"]:
            e["prs"].append(f["url"])
    return {
        "view": view, "days": config.get("merged_days"), "merged": len(merged),
        "first_review_h": med([hours(p["created"], p["first_review"]) for p in ps if p.get("first_review")]),
        "merge_h": med([hours(p["created"], p["merged"]) for p in merged]),
        "open": sum(1 for p in ps if not p["merged"] and p["state"] == "OPEN"),
        "waiting": [{"url": p["url"], "title": p["title"], "repo": p["repo"], "number": p["number"], "author": p["author"], "hours": round(h)}
                    for p, h in waiting[:8]],
        "flaky": sorted(by_check.values(), key=lambda e: (-e["count"], -e["last"]))[:15],
        "flaky_total": sum(counts.values()),
    }


def test_integration(what):
    if what == "slack":
        who = integrations.slack("auth.test")
        return f'Connected to {who.get("team")} as {who.get("user")}'
    if what == "jira":
        me = integrations.jira("/rest/api/3/myself")
        return f'Connected as {me.get("displayName")}'
    if what == "ai":
        reply = ai.complete("Reply with exactly the word OK and nothing else.", "ping", fast=True, timeout=120)
        return f"{ai.name()} answered: {reply[:80]}"
    if what == "notify":
        sync.notify([(None, "test", "prdash test notification")])
        return "Sent (if a notify command is set)"
    raise ValueError(what)


def setup_info():
    try:
        orgs = github.gh("api", "user/orgs", "--paginate", "--jq", ".[].login").split()
    except RuntimeError:
        orgs = []

    return {"me": github.viewer(), "orgs": orgs, "selected": github.owners(), "dirs": local.find_clone_dirs(),
            "repo_dirs": config.get("repo_dirs"), "terminal": config.get("terminal"), "editor": config.get("editor"),
            "notify_cmd": config.get("notify_cmd"),
            "ai": ai_status()}


def ai_status():
    if ai.provider() in ai.CLIS:
        cmd = config.expand(config.get("ai_cmd").strip() or ai.provider())
        ready = bool(cmd and shutil.which(cmd[0]))
    else:
        ready = bool(config.get("api_base_url") and config.get("api_model"))
    return {"name": ai.name(), "provider": ai.provider(), "ready": ready, "installed": {c: bool(shutil.which(c)) for c in ai.CLIS},
            "api_base_url": config.get("api_base_url"), "api_model": config.get("api_model"), "has_key": bool(config.get("api_key"))}


def setup_save(b):
    config.put("orgs", ",".join(b.get("orgs") or []))
    for key in ("repo_dirs", "terminal", "editor"):
        if b.get(key):
            config.put(key, b[key])
    if "notify_cmd" in b:  # empty is a valid choice: no system notifications
        config.put("notify_cmd", b["notify_cmd"].strip())
    for key in ("ai_provider", "api_base_url", "api_model", "api_key"):
        if b.get(key):
            config.put(key, b[key].strip())
    db.put("orgs_auto", None)
    db.put("setup_done", True)
    sync.now()


def standup_data(window):
    if window not in standup.WINDOWS:
        raise ValueError(f"window must be one of {', '.join(standup.WINDOWS)}")
    if DEMO:
        from . import demo
        result = demo.standup(window)
    else:
        result = standup.collect(window)
    me = db.get("viewer")
    return {**result, "text": standup.as_text(result, standup.open_now(me))}


def standup_summary(text):
    if not text.strip():
        raise ValueError("Nothing to summarize.")
    if DEMO:
        from . import demo
        return demo.SUMMARY
    return standup.summarize(text[:40_000])


def theme_attr():
    theme = config.get("theme")
    return f' data-theme="{theme}"' if theme in config.THEMES.split(",") and theme != "system" else ""


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
        # fonts never change; everything else revalidates so upgrades (like new icons) show up right away
        self.send_header("Cache-Control", "max-age=31536000, immutable" if ctype == "font/woff2" else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def session_id(self):
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        name = f"prdash{PORT}"
        return jar[name].value if name in jar else ""

    def authed(self, method="GET"):
        """X-Token header (scripts, `prdash url`) or a browser session from prdash's own page.
        Cookies are shared by every port on localhost and SameSite treats those ports as one site, so a valid
        cookie alone isn't enough: the request must come from this origin, and writes need a custom header,
        which a page on another port can't send without a CORS preflight that prdash never approves."""
        if self.headers.get("X-Token"):
            return secrets.compare_digest(self.headers["X-Token"], token())
        sid = self.session_id()
        if not sid or not valid_session(sid):
            return False
        # fail closed: every current browser sends Sec-Fetch-Site; anything else is not our page
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            return False
        if method == "GET":
            return True
        return (self.headers.get("Origin") in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")
                and secrets.compare_digest(self.headers.get("X-Prdash", ""), csrf(sid)))

    def trusted(self, path, qs, method):
        # Host check blocks DNS rebinding; authed() blocks other pages, including other localhost ports
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        return self.authed(method) if path.startswith("/api/") else True

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
        if not self.trusted(path, qs, method):
            return self.send(403, {"error": "forbidden"})
        try:
            if method == "GET":
                if path == "/":
                    if "key" in qs:  # one-time login link: swap it for the session cookie, then drop it from the URL
                        if not use_login_key(qs["key"][0]):
                            return self.send(401, LOCKED, "text/html; charset=utf-8")
                        self.send_response(303)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", f"prdash{PORT}={new_session()}; Path=/; HttpOnly; SameSite=Strict; "
                                                       f"Max-Age={SESSION_DAYS * 86400}")
                        self.send_header("Set-Cookie", "prdash=; Path=/; Max-Age=0")  # pre-session versions held the token here
                        self.send_header("Content-Length", "0")
                        return self.end_headers()
                    if not valid_session(self.session_id()):
                        return self.send(401, LOCKED, "text/html; charset=utf-8")
                    # the sprite URL carries a content hash so browsers never show a stale icon set after an upgrade
                    sprite = f"/static/icons.svg?v={hashlib.sha1((WEB / 'icons.svg').read_bytes()).hexdigest()[:10]}"
                    page = ((WEB / "index.html").read_text().replace("__CSRF__", csrf(self.session_id()))
                            .replace("/static/icons.svg#", sprite + "#").replace('<html lang="en">', f'<html lang="en" data-icons="{sprite}"{theme_attr()}>'))
                    return self.send(200, page, "text/html; charset=utf-8")
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
                if path == "/api/insights":
                    view = qs.get("view", ["mine"])[0]
                    return self.send(200, insights(view if view in github.VIEWS else "mine"))
                if path == "/api/settings":
                    return self.send(200, settings_data())
                if path == "/api/setup":
                    return self.send(200, setup_info())
                if path == "/api/standup":
                    return self.send(200, standup_data(qs.get("window", ["yesterday"])[0]))
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
                    return self.send(200, {"out": run_op(b.get("url", ""), b.get("op"), b.get("body"), b.get("arg"))})
                if path == "/api/batch":
                    return self.send(200, {"results": self.batch(b)})
                if path == "/api/events/read":
                    db.mark_read(b.get("ids"))
                    return self.send(200, {"ok": True})
                if path == "/api/events/delete":
                    db.delete_events(b.get("ids"))
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
                if path == "/api/standup/summary":
                    return self.send(200, {"summary": standup_summary(b.get("text", ""))})
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
                    out = run_op(url, b.get("op"), b.get("body"), b.get("arg"))
                results.append({"url": url, "ok": True, "out": out})
            except Exception as e:
                results.append({"url": url, "ok": False, "out": str(e)})
        return results

    def page_config(self):
        return {"demo": DEMO, "setup_done": bool(db.get("setup_done")), "ai_name": ai.name(), "theme": config.get("theme"),
                "ai_terminal": ai.interactive("", "/") is not None, "jira_url": config.get("jira_url").rstrip("/"), "slack": integrations.slack_ready(),
                "slack_channel": config.get("slack_channel"), "jira": integrations.jira_ready(),
                "merge_method": config.get("merge_method"), "stale_days": config.get("stale_days"),
                "merged_days": config.get("merged_days"), "team_view": config.get("team_view"),
                "notify_browser": config.get("notify_browser"), "slack_template": config.get("slack_template"),
                "retry_max": config.get("retry_max")}

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
    token()  # run the one-time token rotation now, before any browser session exists
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()
