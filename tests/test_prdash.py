"""python3 -m unittest discover tests   (stdlib only; Slack and Jira run against local stub servers)"""
import json, os, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

os.environ["PRDASH_HOME"] = tempfile.mkdtemp(prefix="prdash-test-")
os.environ["PRDASH_ORGS"] = "acme"
os.environ.pop("PRDASH_JIRA", None)

from prdash import config, db, github, integrations, server, sync  # noqa: E402

NODE = {
    "id": "PR_1", "url": "https://github.com/acme/web/pull/1", "number": 1, "title": "[ABC-12] Fix", "isDraft": False,
    "state": "OPEN", "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z", "mergedAt": None, "closedAt": None,
    "additions": 1, "deletions": 2, "changedFiles": 1, "headRefName": "fix/ABC-12-thing", "baseRefName": "main",
    "mergeable": "MERGEABLE", "reviewDecision": None, "author": None, "mergedBy": None,
    "repository": {"name": "web", "owner": {"login": "acme"}}, "labels": {"nodes": []}, "comments": {"totalCount": 0, "nodes": []},
    "reviewRequests": {"nodes": [{"requestedReviewer": {"login": "alice"}}, {"requestedReviewer": {"slug": "devs"}}]},
    "latestReviews": {"nodes": [{"author": {"login": "alice"}, "state": "APPROVED", "submittedAt": "x"}]},
    "commits": {"nodes": []},
}


def pr(**over):
    return {**github.flatten(NODE), **over}


class Status(unittest.TestCase):
    def test_flatten(self):
        p = github.flatten(NODE)
        self.assertEqual(p["jira"], ["ABC-12"])
        self.assertEqual(p["author"], "ghost")
        self.assertEqual(p["reviewers"], [{"who": "alice", "state": "APPROVED"}, {"who": "devs", "state": "REQUESTED"}])
        self.assertEqual(p["status"], "in_review")

    def test_status_order(self):
        cases = [({"merged": "x", "draft": True}, "merged"), ({"draft": True, "ci": "FAILURE"}, "draft"),
                 ({"review": "APPROVED", "ci": "FAILURE"}, "needs_work"), ({"review": "APPROVED", "mergeable": "CONFLICTING"}, "needs_work"),
                 ({"review": "CHANGES_REQUESTED"}, "needs_work"), ({"review": "APPROVED", "ci": "PENDING"}, "approved"),
                 ({"review": "REVIEW_REQUIRED", "ci": "SUCCESS"}, "in_review")]
        for over, want in cases:
            self.assertEqual(github.status(pr(**over)), want, over)

    def test_bump_and_urgent(self):
        c = lambda body, who="bob", at="2026-10-05T00:00:00Z": {"body": body, "createdAt": at, "author": {"login": who}}
        self.assertEqual(github.bump([c("bump"), c("LGTM")]), {"who": "bob", "at": "2026-10-05T00:00:00Z"})
        self.assertEqual(github.bump([c("Friendly bump 🙏", "dana", "2026-10-06T00:00:00Z")])["who"], "dana")
        self.assertIsNone(github.bump([c("We should bump the version before release and update the changelog too")]),
                          "long comments that mention bumping a version aren't bumps")
        self.assertIsNone(github.bump([c("Looks good")]))
        self.assertTrue(github.flatten({**NODE, "title": "URGENT: fix login"})["urgent"])
        self.assertTrue(github.flatten({**NODE, "labels": {"nodes": [{"name": "urgent"}]}})["urgent"])
        self.assertFalse(github.flatten(NODE)["urgent"])

    def test_allowed(self):
        self.assertTrue(github.allowed("https://github.com/acme/web/pull/1"))
        self.assertTrue(github.allowed("https://github.com/ACME/web/pull/1"))
        for bad in ("https://github.com/other/web/pull/1", "--admin", "https://github.com/acme/web/pull/1 --admin",
                    "https://evil.com/acme/web/pull/1"):
            self.assertFalse(github.allowed(bad), bad)


class Inbox(unittest.TestCase):
    def test_diff_events(self):
        u = NODE["url"]
        old = {u: pr(author="me", ci="PENDING", comments=1, reviewers=[{"who": "alice", "state": "REQUESTED"}])}
        new = {u: pr(author="me", ci="FAILURE", comments=3, mergeable="CONFLICTING",
                     reviewers=[{"who": "alice", "state": "CHANGES_REQUESTED"}])}
        kinds = [k for _, k, _ in sync.diff(old, new, "me", set(), set())]
        self.assertEqual(kinds, ["ci_failed", "review", "comment", "conflict"])
        merged = {u: pr(author="me", merged="2026-10-03", merged_by="alice")}
        self.assertEqual([k for _, k, _ in sync.diff(old, merged, "me", set(), set())], ["merged"])

    def test_diff_ignores_others_and_new(self):
        u = NODE["url"]
        self.assertEqual(sync.diff({u: pr(author="bob")}, {u: pr(author="bob", ci="FAILURE")}, "me", set(), set()), [])
        self.assertEqual(sync.diff({}, {u: pr(author="me", ci="FAILURE")}, "me", set(), set()), [])

    def test_bump_and_urgent_events(self):
        u = NODE["url"]
        b = {"who": "bob", "at": "2026-10-05T00:00:00Z"}
        kinds = lambda old, new, review=set(): [k for _, k, _ in sync.diff(old, new, "me", review, review)]
        self.assertEqual(kinds({u: pr(author="me")}, {u: pr(author="me", bump=b)}), ["bump"])
        self.assertEqual(kinds({u: pr(author="me", bump=b)}, {u: pr(author="me", bump=b)}), [], "same bump only once")
        self.assertEqual(kinds({u: pr(author="carol")}, {u: pr(author="carol", bump=b)}, {u}), ["bump"], "bumps in your queue")
        self.assertEqual(kinds({u: pr(author="me")}, {u: pr(author="me", bump={**b, "who": "me"})}), [], "your own bump")
        events = sync.diff({}, {u: pr(author="bob", urgent=True)}, "me", set(), {u})
        self.assertEqual(events[0][1], "urgent")

    def test_review_requested(self):
        u = NODE["url"]
        events = sync.diff({}, {u: pr(author="bob")}, "me", set(), {u})
        self.assertEqual(events[0][1], "review_requested")
        self.assertIn("bob requested your review", events[0][2])


class Storage(unittest.TestCase):
    def test_local_merge_and_delete(self):
        u = "https://github.com/acme/web/pull/9"
        db.local_update(u, {"note": "hi", "prio": 2})
        self.assertEqual(db.local_update(u, {"prio": None, "tags": "x"}), {"note": "hi", "tags": "x"})

    def test_views_drop_orphans(self):
        a, b = "https://github.com/acme/web/pull/2", "https://github.com/acme/web/pull/3"
        db.save_views({"mine": [a, b]}, {a: pr(url=a), b: pr(url=b)})
        db.save_views({"mine": [a]}, {})
        prs, views = db.all_prs()
        self.assertIn(a, prs)
        self.assertNotIn(b, prs)
        self.assertEqual(views["mine"], [a])

    def test_events_roundtrip(self):
        db.add_events([("u", "merged", "Merged")])
        ev = db.events(1)[0]
        self.assertEqual((ev["kind"], ev["read"]), ("merged", 0))
        db.mark_read([ev["id"]])
        self.assertEqual(db.events(1)[0]["read"], 1)


class Settings(unittest.TestCase):
    def test_validation_and_reset(self):
        config.put("merge_method", "rebase")
        self.assertEqual(config.get("merge_method"), "rebase")
        with self.assertRaises(ValueError):
            config.put("merge_method", "yolo")
        config.put("merge_method", None)
        self.assertEqual(config.get("merge_method"), "squash")
        config.put("poll_seconds", "3")
        self.assertEqual(config.get("poll_seconds"), 20)
        self.assertEqual(config.get("orgs"), "acme")  # from env

    def test_secrets_never_leave(self):
        config.put("slack_token", "xoxp-secret")
        self.assertNotIn("xoxp-secret", json.dumps(config.public()))
        config.put("slack_token", None)

    def test_expand_cannot_inject_args(self):
        config.put("terminal", "kitty --directory {dir}")
        argv = config.expand("{terminal} nvim .", dir="/tmp/a b; rm -rf ~")
        self.assertEqual(argv, ["kitty", "--directory", "/tmp/a b; rm -rf ~", "nvim", "."])
        config.put("terminal", None)


class Stub(BaseHTTPRequestHandler):
    """Fake Slack + Jira. Records requests; replies from ROUTES."""
    calls, ROUTES = [], {}

    def handle_any(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        Stub.calls.append((self.command, self.path, body, self.headers.get("Authorization")))
        key = next((k for k in Stub.ROUTES if self.path.startswith(k)), None)
        data = json.dumps(Stub.ROUTES[key] if key else {"ok": False, "error": "unknown"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = handle_any

    def log_message(self, *a):
        pass


class Integrations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_port}"
        integrations.SLACK_API = base + "/api"
        config.put("slack_token", "xoxp-test")
        config.put("slack_channel", "#dev")
        config.put("jira_url", base)
        config.put("jira_email", "me@example.com")
        config.put("jira_token", "tok")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        for k in ("slack_token", "slack_channel", "jira_url", "jira_email", "jira_token"):
            config.put(k, None)

    def setUp(self):
        Stub.calls.clear()

    def test_announcement_text(self):
        self.assertEqual(integrations.announcement(pr(title="[ABC-12] Fix the thing")), f"PR to fix the thing: {NODE['url']}")
        self.assertEqual(integrations.announcement(pr(title="ABC-12: Add CI")), f"PR to add CI: {NODE['url']}")
        self.assertEqual(integrations.announcement(pr(title="CVE bump")), f"PR to CVE bump: {NODE['url']}", "acronyms keep case")

    def test_slack_post_records_thread(self):
        Stub.ROUTES = {"/api/chat.postMessage": {"ok": True, "channel": "C1", "ts": "111.2"},
                       "/api/chat.getPermalink": {"ok": True, "permalink": "https://slack/p111"}}
        integrations.slack_post(pr())
        self.assertEqual(db.local_get(NODE["url"])["slack"], {"channel": "C1", "ts": "111.2", "link": "https://slack/p111"})
        method, path, body, auth = Stub.calls[0]
        self.assertEqual(auth, "Bearer xoxp-test")
        self.assertIn("text=PR+to+fix", body)

    def test_slack_find_and_thread(self):
        db.local_update(NODE["url"], {"slack": None, "posted": None})
        Stub.ROUTES = {
            "/api/search.messages": {"ok": True, "messages": {"matches": [
                {"ts": "5.5", "channel": {"id": "C9"}, "permalink": "https://x/p?thread_ts=4.4"}]}},
            "/api/conversations.replies": {"ok": True, "messages": [
                {"ts": "4.4", "text": "PR", "reactions": [{"name": "eyes", "count": 1}]},
                {"ts": "4.5", "user": "U1", "text": "LG but left one comment for <@U2>"}]},
            "/api/users.info": {"ok": True, "user": {"profile": {"display_name": "dana"}}},
        }
        t = integrations.slack_thread(pr())
        self.assertEqual(db.local_get(NODE["url"])["slack"]["ts"], "4.4", "thread root, not the reply")
        self.assertEqual(t["reactions"], [{"name": "eyes", "count": 1}])
        self.assertEqual(t["replies"][0]["who"], "dana")
        self.assertIn("@dana", t["replies"][0]["text"])

    def test_jira_status_and_transition(self):
        Stub.ROUTES = {"/rest/api/3/search/jql": {"issues": [{"key": "ABC-12", "fields": {
            "status": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}, "assignee": {"displayName": "Me"}, "summary": "S"}}]},
            "/rest/api/3/issue/ABC-12/transitions": {"transitions": [{"id": "31", "name": "Finish", "to": {"name": "Done"}}]}}
        self.assertEqual(integrations.jira_issues({"ABC-12", "CVE-2024-1"})["ABC-12"]["status"], "In Review")
        n = len(Stub.calls)
        integrations.jira_issues({"ABC-12", "CVE-2024-1"})
        self.assertEqual(len(Stub.calls), n, "cached, including keys Jira doesn't know")
        integrations.jira_transition("ABC-12", "done")
        self.assertEqual(json.loads(Stub.calls[-1][2]), {"transition": {"id": "31"}})
        with self.assertRaises(RuntimeError):
            integrations.jira_transition("ABC-12", "Nope")


class _NoRedirect(__import__("urllib.request").request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


NO_REDIRECT = __import__("urllib.request").request.build_opener(_NoRedirect)


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 18000 + os.getpid() % 1000
        threading.Thread(target=server.serve, args=(cls.port,), daemon=True).start()
        import time
        time.sleep(0.3)

    def req(self, path, token=None, host=None, body=None, cookie=None, headers=None):
        h = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json", **(headers or {})}
        if token:
            h["X-Token"] = token
        if cookie:
            h["Cookie"] = cookie
        r = Request(f"http://127.0.0.1:{self.port}{path}", headers=h, data=json.dumps(body).encode() if body is not None else None)
        try:
            with NO_REDIRECT.open(r, timeout=5) as resp:
                return resp.status, resp.read()
        except HTTPError as e:
            with e:
                return e.code, e.read()

    def test_security(self):
        tok = server.token()
        self.assertEqual(self.req("/api/events")[0], 403, "no token")
        self.assertEqual(self.req("/api/events", "wrong")[0], 403)
        self.assertEqual(self.req("/api/events", tok)[0], 200)
        self.assertEqual(self.req("/", host="evil.example:80")[0], 403, "DNS rebinding")
        self.assertEqual(self.req("/static/../db.py")[0], 404)
        self.assertEqual(self.req("/static/%2e%2e/db.py")[0], 404)
        self.assertEqual(self.req("/static/js/app.js")[0], 200)
        code, body = self.req("/api/action", tok, body={"url": "https://github.com/other/x/pull/1", "op": "merge"})
        self.assertEqual(code, 400)
        self.assertIn(b"configured orgs", body)

    def test_login_never_exposes_token(self):
        tok = server.token()
        code, body = self.req("/")
        self.assertEqual(code, 401, "no session: locked page")
        self.assertNotIn(tok.encode(), body)
        self.assertEqual(self.req("/?key=guess")[0], 401)
        key = server.login_url(self.port).split("key=")[1]
        r = Request(f"http://127.0.0.1:{self.port}/?key={key}", headers={"Host": f"127.0.0.1:{self.port}"})
        try:
            NO_REDIRECT.open(r, timeout=5)
            self.fail("expected a redirect")
        except HTTPError as e:
            with e:
                self.assertEqual(e.code, 303)
                cookie = e.headers.get_all("Set-Cookie")[0]
                self.assertIn("prdash=; Path=/; Max-Age=0", e.headers.get_all("Set-Cookie"), "legacy cookie cleared")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.req(f"/?key={key}")[0], 401, "login links work once")
        session = cookie.split(";")[0]
        self.assertNotIn(tok, cookie, "the cookie is a session id, not the master token")
        code, body = self.req("/", cookie=session)
        self.assertEqual(code, 200)
        self.assertNotIn(tok.encode(), body, "the page never contains the token")
        same = {"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.req("/api/events", cookie=session, headers=same)[0], 200)
        self.assertEqual(self.req("/api/events", cookie=f"prdash{self.port}=wrong")[0], 403)
        # another localhost port gets the cookie too (browsers don't isolate ports), but that's not enough
        self.assertEqual(self.req("/api/events", cookie=session, headers={"Sec-Fetch-Site": "same-site"})[0], 403)
        self.assertEqual(self.req("/api/sync", cookie=session, body={}, headers={"Origin": "http://127.0.0.1:3000"})[0], 403)
        self.assertEqual(self.req("/api/events", cookie=session)[0], 403, "fails closed without Sec-Fetch-Site")
        self.assertEqual(self.req("/api/sync", cookie=session, body={}, headers=same)[0], 403, "writes need the CSRF token")
        self.assertEqual(self.req("/api/sync", cookie=session, body={}, headers={**same, "X-Prdash": "1"})[0], 403)
        token_meta = body.decode().split('name="csrf" content="')[1].split('"')[0]
        self.assertEqual(self.req("/api/sync", cookie=session, body={}, headers={**same, "X-Prdash": token_meta})[0], 200)
        self.assertEqual(self.req("/api/sync", cookie=session, body={},
                                  headers={"Sec-Fetch-Site": "same-origin", "X-Prdash": token_meta})[0], 403, "writes need Origin")
        server.logout_all()
        self.assertEqual(self.req("/api/events", cookie=session, headers=same)[0], 403, "logout revokes sessions")

    def test_check_links_are_http_only(self):
        self.assertIsNone(github.safe_url("javascript:alert(1)"))
        self.assertIsNone(github.safe_url(" javascript:alert(1)"))
        self.assertEqual(github.safe_url("https://ci.example/run/1"), "https://ci.example/run/1")


if __name__ == "__main__":
    unittest.main()


class Service(unittest.TestCase):
    def test_systemd_unit_render(self):
        from unittest import mock
        from prdash import service
        unit = os.path.join(tempfile.mkdtemp(), "prdash.service")
        calls = []
        with mock.patch.object(service.platform, "system", return_value="Linux"), \
             mock.patch.object(service, "_systemd_unit", return_value=__import__("pathlib").Path(unit)), \
             mock.patch.object(service, "_run", side_effect=lambda *c: calls.append(c)), \
             mock.patch.dict(os.environ, {"PRDASH_ORGS": "acme", "SECRET_THING": "x", "PRDASH_SLACK_TOKEN": "xoxp-1",
                                              "PRDASH_EXTRA_QUERY": 'label:"a b" 100%'}):
            service.install(9999)
        text = open(unit).read()
        self.assertIn('"-m" "prdash" "--no-open" "--port" "9999"', text)
        self.assertIn('Environment="PRDASH_ORGS=acme"', text)
        self.assertIn('Environment="PRDASH_EXTRA_QUERY=label:\\"a b\\" 100%%"', text, "quotes and % escaped")
        self.assertNotIn("SECRET_THING", text, "only PRDASH_* and PATH are copied")
        self.assertNotIn("xoxp-1", text, "secret settings never land in the unit file")
        self.assertEqual(os.stat(unit).st_mode & 0o777, 0o600)
        self.assertEqual(calls[-1], ("systemctl", "--user", "enable", "--now", "prdash.service"))
