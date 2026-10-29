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
    "latestReviews": {"nodes": [{"author": {"login": "alice"}, "state": "APPROVED", "submittedAt": "x"}]}, "latestOpinionatedReviews": {"nodes": []},
    "reviews": {"nodes": []}, "reviewThreads": {"nodes": []}, "commits": {"nodes": []},
}


def pr(**over):
    return {**github.flatten(NODE), **over}


class Status(unittest.TestCase):
    def test_flatten(self):
        p = github.flatten(NODE)
        self.assertEqual(p["jira"], ["ABC-12"])
        self.assertEqual(p["author"], "ghost")
        self.assertEqual(p["reviewers"], [{"who": "alice", "state": "APPROVED", "commit": None}, {"who": "devs", "state": "REQUESTED", "team": True}])
        self.assertEqual(p["status"], "in_review")

    def test_review_commit_skips_thread_replies(self):
        rv = lambda who, state, oid: {"author": {"login": who}, "state": state, "submittedAt": "x", "commit": {"oid": oid}}
        p = github.flatten({**NODE, "latestReviews": {"nodes": [rv("alice", "COMMENTED", "c2"), rv("bob", "COMMENTED", "c3")]},
                            "latestOpinionatedReviews": {"nodes": [rv("alice", "APPROVED", "c1")]}})
        self.assertEqual({r["who"]: r.get("commit") for r in p["reviewers"]}, {"alice": "c1", "bob": "c3", "devs": None},
                         "a reply after approving doesn't move alice's review; bob only commented")

    def test_status_order(self):
        cases = [({"merged": "x", "draft": True}, "merged"), ({"draft": True, "ci": "FAILURE"}, "draft"),
                 ({"review": "APPROVED", "ci": "FAILURE"}, "needs_work"), ({"review": "APPROVED", "mergeable": "CONFLICTING"}, "needs_work"),
                 ({"review": "CHANGES_REQUESTED"}, "needs_work"), ({"review": "APPROVED", "ci": "PENDING"}, "approved"),
                 ({"review": "REVIEW_REQUIRED", "ci": "SUCCESS"}, "in_review")]
        for over, want in cases:
            self.assertEqual(github.status(pr(**over)), want, over)

    def test_blocker(self):
        cases = [({"merge_state": "BEHIND"}, "Behind main"), ({"merge_state": "BLOCKED", "review": "REVIEW_REQUIRED"}, "Needs approval"),
                 ({"merge_state": "BLOCKED", "review": "APPROVED", "ci": "PENDING"}, "Required checks"),
                 ({"merge_state": "BLOCKED", "review": "APPROVED", "ci": "SUCCESS"}, "Blocked by branch rules"),
                 ({"merge_state": "DIRTY"}, None), ({"merge_state": "CLEAN"}, None), ({"merge_state": "BEHIND", "draft": True}, None)]
        for over, want in cases:
            self.assertEqual(github.blocker(pr(**over)), want, over)

    def test_auto_merge(self):
        p = github.flatten({**NODE, "autoMergeRequest": {"mergeMethod": "SQUASH", "enabledBy": {"login": "me"}}, "viewerCanDisableAutoMerge": True})
        self.assertEqual((p["auto_merge"], p["can_automerge_off"]), ({"method": "squash", "by": "me"}, True))
        self.assertIsNone(github.flatten(NODE)["auto_merge"])
        self.assertEqual(github.ACTIONS["automerge"]("u", "")[:4], ["pr", "merge", "u", "--auto"])
        self.assertEqual(github.ACTIONS["automerge_off"]("u", ""), ["pr", "merge", "u", "--disable-auto"])
        self.assertTrue({"automerge", "automerge_off"} <= server.CHANGES_GITHUB, "a sync follows so the card updates")

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

    def test_comment_events_say_who_and_what(self):
        u = NODE["url"]
        text = lambda k, who: [t for _, kind, t in sync.diff({u: pr(author="me", comments=1)},
            {u: pr(author="me", comments=1 + k, last_comment={"who": who, "at": "x", "text": "Can we limit retries?"})},
            "me", set(), set()) if kind == "comment"]
        self.assertEqual(text(1, "alice"), ['alice commented on web#1 [ABC-12] Fix: "Can we limit retries?"'])
        self.assertEqual(text(2, "bob"), ['2 new comments on web#1 [ABC-12] Fix, latest from bob: "Can we limit retries?"'])
        self.assertEqual(text(1, "me"), [])  # your own comment isn't news

    def test_last_comment_and_conversation(self):
        nodes = [{"body": "old", "createdAt": "1", "author": None},
                 {"body": "<!-- template -->\nLooks   good,\nship it", "createdAt": "2", "author": {"login": "alice"}}]
        self.assertEqual(github.last_comment(nodes), {"who": "alice", "at": "2", "text": "Looks good, ship it"})
        self.assertEqual(len(github.last_comment([{**nodes[1], "body": "x" * 500}])["text"]), 140)
        me = {"login": "me"}
        data = {"resource": {"body": "<!-- fill this in -->", "createdAt": "0", "author": me,
                "comments": {"nodes": [{"body": "LGTM", "createdAt": "3", "url": "c1", "author": {"login": "bob"}}]},
                "reviews": {"nodes": [
                    {"state": "COMMENTED", "body": "", "submittedAt": "1", "url": "r1", "author": {"login": "alice"}},
                    {"state": "APPROVED", "body": "", "submittedAt": "2", "url": "r2", "author": {"login": "alice"}}]},
                "reviewThreads": {"nodes": [{"id": "T1", "isResolved": False, "isOutdated": True, "path": "a.py", "line": None, "originalLine": 7,
                    "comments": {"nodes": [{"body": "nit", "createdAt": "1", "url": "i1", "author": {"login": "alice"}},
                                           {"body": "fixed", "createdAt": "1.5", "url": "i2", "author": me}]}}]}}}
        real, github.gql = github.gql, lambda q, **v: data
        try:
            convo = github.conversation(NODE["url"])
        finally:
            github.gql = real
        self.assertEqual([(c["who"], c["what"], c.get("path"), c.get("line")) for c in convo],
                         [("alice", "thread", "a.py", 7), ("alice", "approved", None, None), ("bob", "commented", None, None)])
        self.assertEqual((convo[0]["id"], convo[0]["outdated"], [c["who"] for c in convo[0]["comments"]]), ("T1", True, ["alice", "me"]))

    def test_new_commits_after_changes_requested_go_back_to_review(self):
        node = lambda committed: {**NODE, "reviewDecision": "CHANGES_REQUESTED",
            "latestReviews": {"nodes": [{"author": {"login": "alice"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-10-03T00:00:00Z"}]},
            "commits": {"nodes": [{"commit": {"oid": "abc", "committedDate": committed, "statusCheckRollup": None}}]}}
        before, after = github.flatten(node("2026-10-02T00:00:00Z")), github.flatten(node("2026-10-04T00:00:00Z"))
        self.assertEqual((before["status"], before["fixes_pushed"]), ("needs_work", False))
        self.assertEqual((after["status"], after["fixes_pushed"]), ("in_review", True))
        u = NODE["url"]
        events = sync.diff({u: {**before, "author": "bob", "reviewers": [{"who": "me", "state": "CHANGES_REQUESTED"}]}},
                           {u: {**after, "author": "bob", "reviewers": [{"who": "me", "state": "CHANGES_REQUESTED"}]}}, "me", set(), set())
        self.assertEqual([k for _, k, _ in events], ["pushed"])

    def test_upgrade_has_no_baseline_for_new_fields(self):
        u = NODE["url"]
        old = {k: v for k, v in pr(author="bob").items() if k not in ("mentions", "fixes_pushed")}
        new = pr(author="bob", mentions={"me": {"who": "alice", "at": "1", "text": "@me hi"}}, fixes_pushed=True,
                 reviewers=[{"who": "me", "state": "CHANGES_REQUESTED"}])
        self.assertEqual(sync.diff({u: old}, {u: new}, "me", set(), set()), [], "no flood of old mentions after upgrading")
        edited = {**new, "mentions": {"me": {"who": "alice", "at": "1", "text": "@me hi (edited)"}}}
        self.assertEqual(sync.diff({u: new}, {u: edited}, "me", set(), set()), [], "editing a comment isn't a new mention")

    def test_fix_with_ai_only_on_your_prs(self):
        from prdash import local
        db.put("viewer", "me")
        with self.assertRaisesRegex(ValueError, "your own PRs"):
            local.fix(pr(author="bob"), [{"what": "thread", "resolved": False}])
        convo = [{"who": "alice", "what": "commented", "text": "rename this"}, {"who": "me", "what": "commented", "text": "done"},
                 {"who": "bob", "what": "reviewed", "text": "nit: typo"}, {"who": "alice", "what": "approved", "text": ""},
                 {"who": "me", "what": "opened", "text": "my description"}]
        seen, saved = [], (local.worktree, local.launch, local.ai.interactive)
        local.worktree, local.launch = (lambda p: "/tmp"), (lambda argv, cwd: None)
        local.ai.interactive = lambda prompt, cwd, edit=False: seen.append(prompt) or ["true"]
        try:
            self.assertIn("2 pieces", local.fix(pr(author="me"), convo))
        finally:
            local.worktree, local.launch, local.ai.interactive = saved
        self.assertIn("alice commented:\nrename this", seen[0])
        self.assertIn("bob reviewed:\nnit: typo", seen[0])
        self.assertNotIn("done", seen[0].split("REVIEW FEEDBACK")[1])

    def test_worktree_resets_a_rebased_branch_but_never_loses_commits(self):
        from prdash import local
        d, b = tempfile.mkdtemp(), "fix/ABC-12-thing"
        sh = lambda *a, cwd=d: local.git(*a, cwd=cwd)
        sh("init", "-q", "--bare", "origin.git")
        sh("clone", "-q", "origin.git", "a")
        a, wt = f"{d}/a", f"{d}/wt"
        for args in [("commit", "-q", "--allow-empty", "-m", "base"), ("checkout", "-q", "-b", b),
                     ("commit", "-q", "--allow-empty", "-m", "v1"), ("push", "-q", "origin", "HEAD:main", b),
                     ("commit", "-q", "--amend", "--allow-empty", "-m", "v1 rebased"),
                     ("push", "-q", "-f", "origin", b)]:
            sh("-c", "user.name=t", "-c", "user.email=t@t", *args, cwd=a)
        sh("clone", "-q", "origin.git", "wt")
        sh("branch", "-q", b, "origin/main", cwd=wt)  # stale local copy, nothing GitHub lacks
        calls, local.gh = [], lambda *a, cwd=None: calls.append(a)
        try:
            local.checkout_latest(pr(), wt)
            sh("checkout", "-q", b, cwd=wt)
            open(f"{wt}/f", "w").write("unpushed work")
            sh("add", "f", cwd=wt)
            sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "local only", cwd=wt)
            sh("checkout", "-q", "--detach", cwd=wt)  # now the local copy has a commit GitHub doesn't
            local.checkout_latest(pr(), wt)
        finally:
            local.gh = github.gh
        self.assertEqual([c[-1] for c in calls], ["--force", "1"])

    def test_review_since_your_last_review(self):
        from prdash import ai, local
        db.put("viewer", "me")
        p = pr(author="bob", head="h2", reviewers=[{"who": "me", "state": "APPROVED", "commit": "h1"}])
        with self.assertRaisesRegex(ValueError, "Nothing new"):
            server.start_review({**p, "head": "h1"}, since=True)
        status, sent = ["ahead"], []

        def gh(*a, **k):
            if a[0] != "api":
                return "FULL DIFF" if a[1] == "diff" else "PR TEXT"
            if not status[0]:
                raise RuntimeError("No commit found for SHA: h1")
            return status[0] if "--jq" in a else "COMPARE DIFF"
        convo = [{"who": "me", "what": "approved", "text": "LGTM, but rename x"}, {"who": "alice", "what": "approved", "text": "ok"},
                 {"who": "me", "what": "thread", "path": "a.py", "line": 3, "outdated": False, "comments": [{"who": "me", "text": "off by one"}]}]
        real = local.gh, local.conversation, ai.complete
        local.gh, local.conversation = gh, lambda url: convo
        ai.complete = lambda prompt, data, **k: sent.append((prompt, data)) or "ok"
        try:
            local._run_review(p, lambda pr: None, "h1")
            self.assertIn("at commit h1", sent[-1][0])
            self.assertIn("COMPARE DIFF", sent[-1][1]); self.assertNotIn("FULL DIFF", sent[-1][1])
            self.assertIn("LGTM, but rename x", sent[-1][1]); self.assertIn("off by one", sent[-1][1]); self.assertNotIn("alice", sent[-1][1])
            for status[0] in ("diverged", None):  # rebased or force-pushed, or the reviewed commit is gone: full review
                local._run_review(p, lambda pr: None, "h1")
                self.assertIn("FULL DIFF", sent[-1][1]); self.assertNotIn("MY EARLIER FEEDBACK", sent[-1][1])
                self.assertTrue(db.reviews()[p["url"]]["output"].startswith("The branch was rewritten"))
        finally:
            local.gh, local.conversation, ai.complete = real

    def test_mentions_follow_and_mute(self):
        u = NODE["url"]
        c = [{"body": "@Me can you look? cc @bob, not foo@me.com", "createdAt": "5", "author": {"login": "alice"}}]
        self.assertEqual(set(github.mentions(c)), {"me", "bob"})
        old, new = pr(author="bob"), pr(author="bob", comments=1, mentions=github.mentions(c), ci="FAILURE")
        kinds = lambda local=None: [k for _, k, _ in sync.diff({u: old}, {u: new}, "me", set(), set(), local)]
        self.assertEqual(kinds(), ["mention"])  # someone else's PR: only the mention
        self.assertEqual(kinds({u: {"following": True}}), ["mention", "ci_failed"])  # the comment event folds into the mention
        self.assertEqual(kinds({u: {"following": True, "muted": True}}), [])

    def test_flaky_checks_and_retry_until_green(self):
        u, me = NODE["url"], "me"
        p = pr(author=me, head="h1", merged=None, state="OPEN")
        run = lambda *states: {u: [{"name": f"CI / t{i}", "state": s, "run": 9} for i, s in enumerate(states)]}
        sync.record_checks({u: p}, run("FAILURE", "SUCCESS"))
        sync.record_checks({u: p}, run("IN_PROGRESS", "SUCCESS"))  # re-run in progress keeps the failure in mind
        self.assertEqual(sync.record_checks({u: p}, run("SUCCESS", "SUCCESS")), [(u, "acme/web", "CI / t0")])
        self.assertEqual(sync.record_checks({u: {**p, "head": "h2"}}, run("SUCCESS", "SUCCESS")), [], "a new commit isn't a flake")
        self.assertEqual(db.flake_counts()[("acme/web", "CI / t0")], 1)
        twin = {u: [{"name": "CI / t9", "state": "SUCCESS", "run": 1}, {"name": "CI / t9", "state": "FAILURE", "run": 2}]}
        sync.record_checks({u: p}, twin)
        self.assertEqual(sync.record_checks({u: p}, twin), [], "two runs under one name aren't a flake every sync")

        reruns = []
        real, github.rerun_runs = github.rerun_runs, lambda url, runs: reruns.append(sorted(runs))
        try:
            db.local_update(u, {"retry": {"head": "h1", "attempts": 1, "max": 2, "at": 0}})
            step = lambda states, head="h1": sync.retries({u: {**p, "head": head}}, run(*states), me, db.local_all())
            self.assertEqual(step(["FAILURE"]), []); self.assertEqual(reruns, [[9]])
            self.assertEqual(db.local_get(u)["retry"]["attempts"], 2)
            db.local_update(u, {"retry": {**db.local_get(u)["retry"], "at": 0}})
            self.assertEqual(step(["IN_PROGRESS"]), [], "waits while the re-run is going")
            self.assertEqual([k for _, k, _ in step(["FAILURE"])], ["retry_failed"], "gives up at max attempts")
            self.assertNotIn("retry", db.local_get(u)); self.assertEqual(db.local_get(u)["retry_done"], "h1")
            db.local_update(u, {"retry": {"head": "h1", "attempts": 1, "max": 3, "at": 0}})
            self.assertIn("after 1 re-run:", step(["SUCCESS"])[0][2])
            db.local_update(u, {"retry": {"head": "h1", "attempts": 1, "max": 3, "at": 0}})
            self.assertEqual(step(["FAILURE"], head="h2"), []); self.assertNotIn("retry", db.local_get(u), "new commits stop it quietly")
            config.put("retry_flaky", True)
            db.local_update(u, {"retry_done": None})
            db.local_update(u, {"retry": {"head": "h1", "attempts": 1, "max": 3, "at": 0}})
            self.assertEqual([k for _, k, _ in step(["CANCELLED"])], [], "cancelled counts as failed, so it re-runs")
            self.assertEqual(reruns[-1], [9])
            db.local_update(u, {"retry": None, "retry_done": None})
            self.assertEqual(step(["FAILURE", "SUCCESS"]), [], "one flake isn't a pattern yet")
            db.save_checks([], [(u, "acme/web", "CI / t0")])
            self.assertEqual([k for _, k, _ in step(["FAILURE", "SUCCESS"])], ["retrying"], "two flakes: auto-retry kicks in")
            server.stop_retry({**p, "url": u})
            self.assertEqual(step(["FAILURE", "SUCCESS"]), [], "Stop sticks for this commit even with auto-retry on")
        finally:
            github.rerun_runs = real
            config.put("retry_flaky", None)
            db.local_update(u, {"retry": None, "retry_done": None})

    def test_request_review(self):
        u, calls = NODE["url"], []
        real = github.gh
        github.gh = lambda *a, **k: calls.append(a) or ""
        try:
            self.assertEqual(github.request(pr(), ["bob"], ["devs"]), "requested review from devs, bob")
            self.assertEqual(calls[-1][-4:], ("-f", "reviewers[]=bob", "-f", "team_reviewers[]=devs"))
            with self.assertRaisesRegex(ValueError, "already a reviewer"):
                github.rerequest(pr(), ["carol"])
            self.assertEqual(github.rerequest(pr(), ["alice"]), "re-requested review from alice")
            with self.assertRaisesRegex(ValueError, "Pick someone"):
                server.request_review(pr(), {"users": ["bad name;"]})
            self.assertIn("once CI is green", server.request_review(pr(ci="FAILURE", head="h1"), {"teams": ["devs"], "slack": True, "green": True}))
            self.assertEqual(db.get("request_prefs")["acme"], {"teams": ["devs"], "users": [], "slack": True}, "remembered for the owner")
            self.assertEqual(db.local_get(u)["request_when_green"]["skip"], "h1", "armed on a red PR: waits for the next push")
        finally:
            github.gh = real
            db.local_update(u, {"request_when_green": None})

    def test_request_when_green(self):
        u, did = NODE["url"], []
        real = github.request, github.act, integrations.slack_post
        github.act = lambda op, url, body="": did.append(op)
        github.request = lambda pr, users, teams=(): did.append("request") or "requested review from devs"
        integrations.slack_post = lambda pr: did.append("slack") or "posted to #prs"
        arm = lambda **r: db.local_update(u, {"request_when_green": {"teams": ["devs"], "users": [], "slack": False, "skip": None, **r}})
        step = lambda states, **p: sync.requests_when_green({u: pr(head="h2", **p)}, {u: [{"name": "t", "state": s, "run": 1} for s in states]},
                                                           db.local_all())
        armed = lambda: "request_when_green" in db.local_get(u)
        try:
            arm(skip="h2")
            self.assertEqual(step(["FAILURE"]), []); self.assertTrue(armed(), "failures on the head it was armed on don't count")
            arm()
            self.assertEqual(step(["IN_PROGRESS", "SUCCESS"]), []); self.assertTrue(armed(), "waits while checks run")
            db.put("ci_repos", ["acme/web"])
            self.assertEqual(step([]), []); self.assertTrue(armed(), "no checks yet in a repo with CI: waits")
            db.local_update(u, {"retry": {"head": "h2"}})
            self.assertEqual(step(["FAILURE"]), []); self.assertTrue(armed(), "Retry until green gets its chance")
            db.local_update(u, {"retry": None})
            self.assertEqual([k for _, k, _ in step(["FAILURE"])], ["request_failed"]); self.assertFalse(armed())
            arm(slack=True)
            self.assertEqual(step(["SUCCESS", "SKIPPED"], draft=True)[0][2], "Marked ready, requested review from devs and posted to #prs: web#1 [ABC-12] Fix")
            self.assertEqual(did, ["ready", "request", "slack"]); self.assertFalse(armed())
            db.put("ci_repos", [])
            arm()
            self.assertEqual([k for _, k, _ in step([])], ["requested"], "a repo that never had CI counts as passing")
            arm(slack=True)
            integrations.slack_post = lambda pr: (_ for _ in ()).throw(OSError("timed out"))
            self.assertIn("requested review from devs, then failed: timed out", step(["SUCCESS"])[0][2], "Slack down doesn't break the sync")
            arm()
            github.request = lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 422"))
            self.assertIn("failed: HTTP 422", step(["SUCCESS"])[0][2]); self.assertFalse(armed())
            arm()
            self.assertEqual(step(["SUCCESS"], merged="x"), []); self.assertFalse(armed(), "merged: stops quietly")
        finally:
            github.request, github.act, integrations.slack_post = real
            db.local_update(u, {"request_when_green": None, "retry": None})
            db.put("ci_repos", None)

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


class AIProviders(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp()

    def tearDown(self):
        for k in ("ai_provider", "ai_cmd", "ai_model", "ai_fast_model", "api_base_url", "api_key", "api_model", "ai_interactive_cmd"):
            config.put(k, None)

    def fake_cli(self, name, script):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys, json\n" + script)
        os.chmod(path, 0o755)
        return path

    def test_cli_argv_are_read_only(self):
        from prdash import ai
        claude = ai.CLIS["claude"][1]("P", "haiku", "/repo/x")
        self.assertEqual(claude[:4], ["-p", "P", "--permission-mode", "dontAsk"])
        self.assertEqual(claude[-4:], ["--add-dir", "/repo/x", "--allowedTools", "Read(//repo/x/**)"])
        self.assertNotIn("--add-dir", ai.CLIS["claude"][1]("P", "", None))
        for flag in ("--strict-mcp-config", "WebFetch", "Bash"):
            self.assertIn(flag, claude)
        self.assertEqual(claude[claude.index("--setting-sources") + 1], "user")
        self.assertEqual(ai.CLIS["codex"][1]("P", "", None)[:3], ["exec", "-s", "read-only"])

    def test_headless_never_runs_in_the_checkout(self):
        from prdash import ai
        repo = tempfile.mkdtemp()
        config.put("ai_provider", "claude")
        config.put("ai_cmd", self.fake_cli("claude", "import os; print(os.getcwd())"))
        self.assertNotEqual(ai.complete("p", "d", checkout=repo), repo, "a PR's .claude/.gemini config must not load")

    def test_cli_complete_passes_data_on_stdin(self):
        from prdash import ai
        config.put("ai_provider", "claude")
        config.put("ai_cmd", self.fake_cli("claude", "print('got ' + str(len(sys.stdin.read())) + ' chars, model ' + sys.argv[sys.argv.index('--model') + 1])"))
        config.put("ai_fast_model", "haiku")
        self.assertEqual(ai.complete("summarize", "x" * 42, fast=True), "got 42 chars, model haiku")

    def test_codex_reads_answer_file(self):
        from prdash import ai
        config.put("ai_provider", "codex")
        config.put("ai_cmd", self.fake_cli("codex", "a = sys.argv\nopen(a[a.index('-o') + 1], 'w').write('final answer')\nprint('progress noise')"))
        self.assertEqual(ai.complete("review", "diff"), "final answer")

    def test_api_provider(self):
        from prdash import ai
        Stub.calls.clear()
        Stub.ROUTES = {"/v1/chat/completions": {"choices": [{"message": {"content": "  • shipped x  "}}]}}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            config.put("ai_provider", "api")
            config.put("api_base_url", f"http://127.0.0.1:{srv.server_port}/v1/")
            config.put("api_model", "deepseek-chat")
            config.put("api_key", "sk-test")
            self.assertEqual(ai.complete("summarize", "activity"), "• shipped x")
            _, path, body, auth = Stub.calls[-1]
            self.assertEqual(auth, "Bearer sk-test")
            sent = json.loads(body)
            self.assertEqual((sent["model"], sent["messages"][0]["content"], sent["messages"][1]["content"]), ("deepseek-chat", "summarize", "activity"))
            self.assertIsNone(ai.interactive("p", "/tmp"), "plain APIs have no terminal session unless configured")
            config.put("ai_interactive_cmd", "aider --message {prompt}")
            self.assertEqual(ai.interactive("hi there", "/tmp")[-3:], ["aider", "--message", "hi there"])
        finally:
            srv.shutdown()

    def test_names(self):
        from prdash import ai
        config.put("ai_provider", "api")
        for url, want in (("https://api.deepseek.com", "DeepSeek"), ("https://api.x.ai/v1", "Grok"),
                          ("http://localhost:11434/v1", "Ollama"), ("https://llm.example.com/v1", "llm.example.com")):
            config.put("api_base_url", url)
            self.assertEqual(ai.name(), want)
        config.put("ai_provider", "codex")
        self.assertEqual(ai.name(), "Codex")


class Update(unittest.TestCase):
    def test_fast_forward_only_when_clean(self):
        import subprocess
        from unittest import mock
        from prdash import update
        root = tempfile.mkdtemp()
        git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True)
        git("init", "-q", "-b", "main", f"{root}/origin")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one", cwd=f"{root}/origin")
        git("clone", "-q", f"{root}/origin", f"{root}/clone")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "two", cwd=f"{root}/origin")
        with mock.patch.object(update, "ROOT", __import__("pathlib").Path(f"{root}/clone")):
            open(f"{root}/clone/f.txt", "w").write("x")
            git("add", "f.txt", cwd=f"{root}/clone")
            self.assertIsNone(update.from_git(), "never pulls over local changes")
            git("reset", "-q", "--hard", cwd=f"{root}/clone")
            self.assertIn("updated", update.from_git())
            self.assertIsNone(update.from_git(), "already current")
