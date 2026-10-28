"""`prdash --demo`: a fictional org so you can try prdash (and we can take screenshots) without touching GitHub."""
import random, time
from datetime import datetime, timedelta, timezone

from . import config, db, github

ME = "octo-dev"
PEOPLE = ["maya-chen", "theo-park", "priya-n", "sam-ortiz", "jordan-lee", "alex-kim", "rin-sato"]
REPOS = ["api", "web", "mobile", "infra", "billing", "docs"]
TITLES = [
    "Retry webhook deliveries with exponential backoff", "Add dark mode to the settings page", "Cache invoice PDFs in object storage",
    "Fix race in session refresh", "Migrate cron jobs to the queue worker", "Bump TypeScript to 5.6", "Rate-limit the public search API",
    "Paginate the audit log export", "Show billing usage per workspace", "Drop the legacy v1 auth endpoints", "Speed up cold starts on mobile",
    "Add Terraform module for the read replica", "Validate refund amounts against the original charge", "Stream CSV exports instead of buffering",
    "Fix timezone bug in scheduled reports", "URGENT: Roll back the broken session cookie change", "Document the webhook signature scheme", "Reduce p95 latency on checkout",
    "Lazy-load the analytics dashboard", "Handle 429s from the payment provider", "Add feature flags for the new editor",
    "Move secrets to the vault provider", "Fix flaky end-to-end test for invites", "Support multiple currencies on invoices",
    "Add CODEOWNERS for infra", "Upgrade Postgres client and pool settings", "Show offline banner in the mobile app", "Trim Docker image by 300MB",
    "Add retries to the email sender", "Make search results keyboard navigable", "Deduplicate events in the ingest pipeline",
    "Add per-tenant rate limit overrides", "Fix memory leak in the websocket gateway", "Archive stale workspaces nightly",
    "Add OpenAPI spec for the billing API", "Improve error messages on failed uploads", "Encrypt backups at rest", "Polish onboarding emails",
]
LABELS = [[], [], ["bug"], ["enhancement"], ["security"], ["dependencies"], ["needs-design"], ["performance"]]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_prs():
    rnd, now = random.Random(7), datetime.now(timezone.utc)
    prs = {}
    for i, title in enumerate(TITLES):
        author = ME if i % 3 == 0 else rnd.choice(PEOPLE)
        repo = REPOS[i % len(REPOS)]
        n = 120 + i * 7
        created = now - timedelta(days=rnd.uniform(0.2, 30))
        updated = min(now, created + timedelta(days=rnd.uniform(0, 6)))
        kind = ["draft", "needs_ci", "changes", "conflict", "review", "review", "review", "approved", "approved", "merged"][i % 10]
        merged = iso(now - timedelta(days=rnd.uniform(0.1, 6))) if kind == "merged" else None
        reviewers = [{"who": who, "state": "REQUESTED"} for who in rnd.sample([p for p in PEOPLE if p != author], 2)]
        if kind in ("approved", "merged"):
            reviewers[0]["state"] = "APPROVED"
        if kind == "changes":
            reviewers[0]["state"] = "CHANGES_REQUESTED"
        if author != ME and i % 2:
            reviewers.append({"who": ME, "state": "REQUESTED"})
        key = f"ENG-{400 + i * 3}" if i % 4 else None
        p = {
            "id": f"DEMO_{i}", "url": f"https://github.com/acme/{repo}/pull/{n}", "number": n,
            "title": f"[{key}] {title}" if key else title, "draft": kind == "draft", "state": "MERGED" if merged else "OPEN",
            "repo": repo, "owner": "acme", "author": author, "created": iso(created), "updated": iso(updated), "closed": merged,
            "merged": merged, "merged_by": "maya-chen" if merged else None,
            "adds": rnd.choice([4, 18, 42, 96, 180, 320, 640, 1250]), "dels": rnd.choice([0, 3, 12, 40, 90, 210]),
            "files": rnd.randint(1, 24), "branch": f"{author.split('-')[0]}/{title.lower().split()[0]}-{n}", "base": "main",
            "mergeable": "CONFLICTING" if kind == "conflict" else "MERGEABLE",
            "review": {"approved": "APPROVED", "merged": "APPROVED", "changes": "CHANGES_REQUESTED"}.get(kind, "REVIEW_REQUIRED"),
            "ci": {"needs_ci": "FAILURE", "draft": "PENDING"}.get(kind, rnd.choice(["SUCCESS", "SUCCESS", "SUCCESS", "PENDING"])),
            "labels": rnd.choice(LABELS), "comments": rnd.randint(0, 9), "jira": [key] if key else [], "reviewers": reviewers,
            "bump": {"who": rnd.choice(PEOPLE), "at": iso(now - timedelta(hours=rnd.uniform(2, 40)))} if i in (12, 21, 22) else None,
            "urgent": bool(github.URGENT.search(title)),
            "requested": [r["who"] for r in reviewers if r["state"] == "REQUESTED"],
            "head": f"{i:02d}" + "c0ffee" * 6, "pushed": iso(updated), "fixes_pushed": i in (2, 12),
            "first_review": iso(created + timedelta(hours=rnd.uniform(1, 30))) if kind not in ("draft", "review") or i % 3 else None,
            "open_threads": {2: 2, 12: 1, 15: 1, 6: 1}.get(i, 0), "mentions": {},
        }
        if p["fixes_pushed"]:
            p["reviewers"][0]["state"] = "CHANGES_REQUESTED"
            p["review"] = "CHANGES_REQUESTED"
        p["status"] = github.status(p)
        prs[p["url"]] = p
    # a three-PR stack in api: each one targets the branch of the one below it
    api = [p for p in prs.values() if p["repo"] == "api" and not p["merged"] and p["author"] == ME][:3]
    for below, above in zip(api, api[1:]):
        above["base"] = below["branch"]
    return prs


def label(p):
    return f"{p['repo']}#{p['number']} {p['title']}"


def seed():
    """Fill the (temporary) database with the demo org. Called with PRDASH_HOME pointing at a scratch dir."""
    prs = make_prs()
    mine = [u for u, p in prs.items() if p["author"] == ME]
    review = [u for u, p in prs.items() if any(r["who"] == ME and r["state"] == "REQUESTED" for r in p["reviewers"]) and not p["merged"]]
    db.save_views({"mine": mine, "review": review, "team": list(prs)}, prs)
    db.put("viewer", ME)
    db.put("orgs_auto", ["acme"])
    db.put("setup_done", True)
    db.put("synced_once", True)
    config.put("orgs", "acme")
    config.put("ai_provider", "claude")  # stable labels in screenshots, even on machines without the CLI
    config.put("jira_url", "https://acme.atlassian.net")
    urls = list(prs)
    notes = {urls[0]: {"prio": 3, "waiting": "Maya", "note": "Ship before Friday's release"}, urls[3]: {"tags": "blocked, needs-qa"},
             urls[6]: {"prio": 2, "posted": iso(datetime.now(timezone.utc))}, urls[9]: {"prio": 1, "tags": "follow-up"},
             urls[12]: {"waiting": "QA", "note": "Repro steps in the ticket"}}
    for url, fields in notes.items():
        db.local_update(url, fields)
    statuses = [("In Progress", "indeterminate"), ("In Review", "indeterminate"), ("To Do", "new"), ("Done", "done")]
    db.put("jira_cache", {k: {"status": s, "category": c, "assignee": "Octo Dev", "summary": prs[u]["title"], "at": time.time() + 1e9}
                          for i, (u, (s, c)) in enumerate(zip(urls, statuses * 20)) for k in prs[u]["jira"]})
    db.review_set(urls[6], status="done", started=time.time() - 400, finished=time.time() - 300, output=(
        "Summary: Adds per-tenant overrides on top of the global rate limiter, read from the tenants table and cached for 60s.\n\n"
        "Issues:\n1. api/limits.py:88 reads the override on every request when the cache misses under load; "
        "two pods can stampede the DB. Use a single-flight lock or a short negative cache.\n"
        "2. No test covers an override lower than the global default.\n\n"
        "Nits:\n- Rename `ovr` to `override` in limits.py:41.\n\n"
        "Verdict: comment. Solid change; fix the cache stampede before merging."))
    db.local_update(urls[1], {"retry": {"head": prs[urls[1]]["head"], "attempts": 1, "max": 3, "at": time.time()}})
    db.save_checks([], [(urls[i], f"acme/{prs[urls[i]]['repo']}", name) for i, name in
                        [(1, "CI / unit tests"), (1, "CI / unit tests"), (11, "CI / unit tests"), (4, "CI / e2e"), (14, "CI / e2e"),
                         (6, "CI / e2e"), (21, "CI / e2e"), (8, "CI / lint")]])
    db.add_events([(urls[7], "review", "maya-chen approved web#169 Validate refund amounts against the original charge"),
                   (urls[4], "mention", f'theo-park mentioned you on {label(prs[urls[4]])}: "@octo-dev does the queue worker need the new env var too?"'),
                   (urls[2], "pushed", f"{prs[urls[2]]['author']} pushed new commits after your change request: {label(prs[urls[2]])}"),
                   (urls[1], "retrying", f"Retrying known-flaky checks: {label(prs[urls[1]])}"),
                   (urls[1], "ci_failed", "CI failed: web#127 Add dark mode to the settings page"),
                   (urls[4], "review_requested", "theo-park requested your review: infra#148 Migrate cron jobs to the queue worker"),
                   (urls[6], "review_done", "Claude review ready: api#162 Add per-tenant rate limit overrides"),
                   (urls[9], "merged", "Merged by maya-chen: infra#183 Drop the legacy v1 auth endpoints"),
                   (urls[12], "bump", f"{prs[urls[12]]['bump']['who']} bumped {label(prs[urls[12]])}"),
                   (urls[22], "bump", f"{prs[urls[22]]['bump']['who']} bumped {label(prs[urls[22]])}")])


def detail(pr):
    checks = [("build", "SUCCESS"), ("lint", "SUCCESS"), ("unit tests", "FAILURE" if pr["ci"] == "FAILURE" else "SUCCESS"),
              ("e2e", "IN_PROGRESS" if pr["ci"] == "PENDING" else "SUCCESS"), ("CodeQL", "SUCCESS")]
    at = lambda h: iso(datetime.now(timezone.utc) - timedelta(hours=h))
    reviewer = (pr["reviewers"] or [{"who": "maya-chen"}])[0]["who"]
    convo = [{"who": pr["author"], "what": "opened", "at": pr["created"], "url": pr["url"],
              "text": "## Description\n\nWebhook deliveries that hit a 429 or 5xx are now retried with exponential backoff, capped at "
                      "**5 minutes**. Retries are tracked per endpoint in `webhook_attempts`, so a slow receiver can't starve the queue.\n\n"
                      "Changes include:\n\n- [x] Backoff with jitter in `deliver()`\n- [x] Give up after 8 attempts and mark the delivery failed\n"
                      "- [ ] Dashboard for failed deliveries (follow-up)\n\n## Testing\n\nTested against staging with a receiver that returns 503 "
                      "for 10 minutes. Screenshots are in [the ticket](https://acme.atlassian.net/browse/ENG-418)."},
             {"who": "priya-n", "what": "commented", "at": at(30), "url": pr["url"] + "#c1",
              "text": "Do we have numbers on how often this fires in prod? Want to make sure the backoff cap is right."},
             {"who": pr["author"], "what": "commented", "at": at(26), "url": pr["url"] + "#c2",
              "text": "About 40 a day, mostly during the nightly sync. The cap is 5 minutes, which covers the longest outage we've seen."},
             {"who": reviewer, "what": "thread", "at": at(20), "url": pr["url"] + "#r1", "id": "T1", "resolved": False, "outdated": False,
              "path": f"{pr['repo']}/handlers.py", "line": 88, "comments": [
                  {"who": reviewer, "at": at(20), "url": pr["url"] + "#r1", "text": "This retries on every 4xx too. Can we limit it to 429 and 5xx?"},
                  {"who": pr["author"], "at": at(18), "url": pr["url"] + "#r2", "text": "Good catch. Fixed in the latest push, and added a test for 404."}]},
             {"who": "priya-n", "what": "thread", "at": at(19), "url": pr["url"] + "#r4", "id": "T2", "resolved": True, "outdated": True,
              "path": f"{pr['repo']}/backoff.py", "line": 12, "comments": [
                  {"who": "priya-n", "at": at(19), "url": pr["url"] + "#r4", "text": "nit: `MAX_DELAY` reads better than `CAP`."},
                  {"who": pr["author"], "at": at(17), "url": pr["url"] + "#r5", "text": "Renamed."}]},
             {"who": reviewer, "what": "approved", "at": at(3), "url": pr["url"] + "#r3", "text": "Looks good once CI is green."}]
    flakes = db.flake_counts()
    return {"checks": [{"name": f"CI / {n}", "state": s, "url": pr["url"] + "/checks", "run": 1,
                        "flakes": flakes.get((f"acme/{pr['repo']}", f"CI / {n}"), 0)} for n, s in checks],
            "conversation": convo,
            "git": {"clone": f"~/code/{pr['repo']}", "worktree": None, "branch": "main", "dirty": False, "on_pr": False}}


SUMMARY = """• Shipped the legacy v1 auth endpoint removal (infra#183), approved by Maya.
• Opened and marked ready the urgent session cookie rollback (infra#225); it needs a reviewer today.
• Refund amount validation (api#204) has changes requested by Theo; pushing fixes next.
• Rate limiting for the public search API (api#162) is approved by Priya and ready to merge.
• Reviewed the cron-to-queue migration and the flaky invite test fix, both approved."""

# standup activity per window: (index into the demo PRs, what happened)
STANDUP = {
    "yesterday": [(15, dict(commits=2, ready=True)), (12, dict(commits=1, changes_by=["theo-park"])), (4, "reviewed")],
    "this_week": [(9, dict(commits=3, approved_by=["maya-chen"], merged=True)), (15, dict(opened=True, commits=4, ready=True)),
                  (12, dict(commits=2, changes_by=["theo-park"])), (6, dict(commits=1, approved_by=["priya-n"])),
                  (4, "reviewed"), (22, "reviewed")],
    "last_week": [(9, dict(opened=True, commits=6)), (18, dict(commits=2, approved_by=["maya-chen"], merged=True)),
                  (24, dict(commits=4, approved_by=["sam-ortiz"], merged=True)), (6, dict(opened=True, commits=5, ready=True)),
                  (0, dict(opened=True)), (27, dict(closed=True)), (4, "reviewed"), (13, "reviewed"), (19, "reviewed"), (25, "reviewed")],
}


def standup(name):
    from .standup import WINDOWS, window
    start, end = window(name)
    prs, _ = db.all_prs()
    urls = list(make_prs())
    items = []
    for i, what in STANDUP[name]:
        p = prs.get(urls[i])
        if not p:
            continue
        base = {"url": p["url"], "repo": p["repo"], "number": p["number"], "title": p["title"], "author": p["author"],
                "draft": p["draft"], "state": p["state"]}
        if what == "reviewed":
            items.append({**base, "role": "reviewed", "states": ["APPROVED" if i % 2 == 0 else "CHANGES_REQUESTED"]})
        else:
            items.append({**base, "role": "mine", "opened": False, "commits": 0, "ready": False, "drafted": False,
                          "approved_by": [], "changes_by": [], "merged": False, "merged_by": "maya-chen", "closed": False, **what})
    return {"window": name, "label": WINDOWS[name], "start": start.isoformat(), "end": end.isoformat(), "items": items}
