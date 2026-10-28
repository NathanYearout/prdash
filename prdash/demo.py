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
    "Fix timezone bug in scheduled reports", "Add SSO login with SAML", "Document the webhook signature scheme", "Reduce p95 latency on checkout",
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
        }
        p["status"] = github.status(p)
        prs[p["url"]] = p
    return prs


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
    db.add_events([(urls[7], "review", "maya-chen approved web#169 Validate refund amounts against the original charge"),
                   (urls[1], "ci_failed", "CI failed: web#127 Add dark mode to the settings page"),
                   (urls[4], "review_requested", "theo-park requested your review: infra#148 Migrate cron jobs to the queue worker"),
                   (urls[6], "review_done", "Claude review ready: api#162 Add per-tenant rate limit overrides"),
                   (urls[9], "merged", "Merged by maya-chen: infra#183 Drop the legacy v1 auth endpoints")])


def detail(pr):
    checks = [("build", "SUCCESS"), ("lint", "SUCCESS"), ("unit tests", "FAILURE" if pr["ci"] == "FAILURE" else "SUCCESS"),
              ("e2e", "IN_PROGRESS" if pr["ci"] == "PENDING" else "SUCCESS"), ("CodeQL", "SUCCESS")]
    return {"checks": [{"name": f"CI / {n}", "state": s, "url": pr["url"] + "/checks", "run": 1} for n, s in checks],
            "git": {"clone": f"~/code/{pr['repo']}", "worktree": None, "branch": "main", "dirty": False, "on_pr": False}}
