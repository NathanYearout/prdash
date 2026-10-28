"""GitHub via the `gh` CLI: which PRs belong to each view, their details, CI checks, and PR actions."""
import json, re, subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import config, db

PR_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
VIEWS = {"mine": "author:@me", "review": "review-requested:@me", "team": ""}

PR_FIELDS = """
  id url number title isDraft state createdAt updatedAt mergedAt closedAt additions deletions changedFiles
  headRefName baseRefName mergeable reviewDecision
  author { login } mergedBy { login }
  repository { name owner { login } }
  labels(first: 10) { nodes { name } }
  comments { totalCount }
  reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { slug } } } }
  latestReviews(first: 15) { nodes { author { login } state submittedAt } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
"""


def gh(*args, cwd=None, timeout=120):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or f"gh {args[0]} failed")
    return r.stdout


def gql(query, **variables):
    r = subprocess.run(["gh", "api", "graphql", "--input", "-"], input=json.dumps({"query": query, "variables": variables}),
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "GitHub GraphQL request failed")
    out = json.loads(r.stdout)
    if out.get("errors") and not out.get("data"):
        raise RuntimeError(out["errors"][0].get("message", "GraphQL error"))
    return out["data"]


def viewer():
    login = db.get("viewer")
    if not login:
        login = gql("{ viewer { login } }")["viewer"]["login"]
        db.put("viewer", login)
    return login


def owners():
    """Orgs to search, from settings or every org you belong to (cached); your own login if you have none."""
    configured = [o.strip() for o in config.get("orgs").split(",") if o.strip()]
    if configured:
        return configured
    cached = db.get("orgs_auto")
    if cached is None:
        cached = gh("api", "user/orgs", "--paginate", "--jq", ".[].login").split()
        db.put("orgs_auto", cached)
    return cached or [viewer()]


def allowed(url):
    return bool(PR_URL.match(url)) and url.split("/")[3].lower() in {o.lower() for o in owners()}


def status(p):
    """Board column. Order matters: merged and draft win over review state."""
    if p["merged"]:
        return "merged"
    if p["draft"]:
        return "draft"
    if p["review"] == "CHANGES_REQUESTED" or p["ci"] in ("FAILURE", "ERROR") or p["mergeable"] == "CONFLICTING":
        return "needs_work"
    if p["review"] == "APPROVED":
        return "approved"
    return "in_review"


def flatten(n):
    commits = n["commits"]["nodes"]
    rollup = commits and commits[0]["commit"]["statusCheckRollup"]
    reviewed = {r["author"]["login"]: r["state"] for r in n["latestReviews"]["nodes"] if r["author"]}
    requested = [rr["requestedReviewer"] for rr in n["reviewRequests"]["nodes"] if rr["requestedReviewer"]]
    requested = [r.get("login") or r.get("slug") for r in requested]
    p = {
        "id": n["id"], "url": n["url"], "number": n["number"], "title": n["title"], "draft": n["isDraft"],
        "state": n["state"], "repo": n["repository"]["name"], "owner": n["repository"]["owner"]["login"],
        "author": (n["author"] or {}).get("login", "ghost"),
        "created": n["createdAt"], "updated": n["updatedAt"], "closed": n["closedAt"],
        "merged": n["mergedAt"], "merged_by": (n["mergedBy"] or {}).get("login"),
        "adds": n["additions"], "dels": n["deletions"], "files": n["changedFiles"],
        "branch": n["headRefName"], "base": n["baseRefName"], "mergeable": n["mergeable"],
        "review": n["reviewDecision"], "ci": rollup["state"] if rollup else None,
        "labels": [l["name"] for l in n["labels"]["nodes"]],
        "comments": n["comments"]["totalCount"],
        "jira": sorted(set(JIRA_KEY.findall(n["title"] + " " + n["headRefName"]))),
        "reviewers": [{"who": k, "state": v} for k, v in reviewed.items()]
        + [{"who": r, "state": "REQUESTED"} for r in requested if r not in reviewed],
    }
    p["status"] = status(p)
    return p


def search_ids(q):
    """[(id, url)] for every PR matching a search, 100 per page."""
    out, cursor = [], None
    while True:
        s = gql("""query($q: String!, $c: String) { search(query: $q, type: ISSUE, first: 100, after: $c) {
                     pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { id url } } } }""", q=q, c=cursor)["search"]
        out += [(n["id"], n["url"]) for n in s["nodes"] if n]
        # ponytail: GitHub search caps at 1000 results; narrow with orgs or extra_query beyond that
        if not s["pageInfo"]["hasNextPage"] or len(out) >= 1000:
            return out
        cursor = s["pageInfo"]["endCursor"]


def membership(view):
    """(open, merged): [(id, url)] for one view."""
    base = " ".join(["is:pr archived:false", *(f"user:{o}" for o in owners()), VIEWS[view], config.get("extra_query")])
    since = (date.today() - timedelta(days=config.get("merged_days"))).isoformat()
    with ThreadPoolExecutor(2) as ex:
        open_ = ex.submit(search_ids, f"{base} is:open")
        merged = ex.submit(search_ids, f"{base} is:merged merged:>={since}") if view != "review" else None
        return open_.result(), merged.result() if merged else []


def fetch(ids):
    """Full details for node ids, 25 per request, 8 requests at a time (~5s for 200 PRs)."""
    chunks = [ids[i:i + 25] for i in range(0, len(ids), 25)]
    query = f"query($ids: [ID!]!) {{ nodes(ids: $ids) {{ ... on PullRequest {{ {PR_FIELDS} }} }} }}"
    with ThreadPoolExecutor(8) as ex:
        pages = list(ex.map(lambda c: gql(query, ids=c)["nodes"], chunks))
    return {p["url"]: p for p in (flatten(n) for page in pages for n in page if n)}


def checks(url):
    """CI checks for the head commit: name, state, link, and the Actions run id when re-runnable."""
    q = """query($u: URI!) { resource(url: $u) { ... on PullRequest { commits(last: 1) { nodes { commit {
             statusCheckRollup { contexts(first: 100) { nodes { __typename
               ... on CheckRun { name status conclusion detailsUrl checkSuite { workflowRun { databaseId workflow { name } } } }
               ... on StatusContext { context state targetUrl } } } } } } } } } }"""
    commits = gql(q, u=url)["resource"]["commits"]["nodes"]
    rollup = commits and commits[0]["commit"]["statusCheckRollup"]
    out = []
    for c in (rollup["contexts"]["nodes"] if rollup else []):
        if c["__typename"] == "CheckRun":
            run = (c["checkSuite"] or {}).get("workflowRun") or {}
            state = (c["conclusion"] or c["status"] or "").upper()
            name = f'{run["workflow"]["name"]} / {c["name"]}' if run.get("workflow") else c["name"]
            out.append({"name": name, "state": state, "url": c["detailsUrl"], "run": run.get("databaseId")})
        else:
            out.append({"name": c["context"], "state": c["state"], "url": c["targetUrl"], "run": None})
    rank = {"FAILURE": 0, "ERROR": 0, "TIMED_OUT": 0, "CANCELLED": 1, "ACTION_REQUIRED": 1, "IN_PROGRESS": 2,
            "QUEUED": 2, "PENDING": 2, "EXPECTED": 2}
    return sorted(out, key=lambda c: (rank.get(c["state"], 3), c["name"]))


def rerun_failed(url):
    failed = {c["run"] for c in checks(url) if c["run"] and c["state"] in ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR")}
    if not failed:
        raise ValueError("No failed GitHub Actions runs to re-run (other CI systems re-run from their own UI).")
    repo = "/".join(url.split("/")[3:5])
    for run in failed:
        gh("run", "rerun", str(run), "--failed", "-R", repo)
    return f"re-running {len(failed)} workflow run(s)"


# op -> gh args. The comment/review body is appended where an op takes one.
ACTIONS = {
    "approve": lambda u, b: ["pr", "review", u, "--approve", *(["-b", b] if b else [])],
    "request_changes": lambda u, b: ["pr", "review", u, "--request-changes", "-b", b or "See comments."],
    "comment": lambda u, b: ["pr", "comment", u, "-b", b],
    "rebase": lambda u, b: ["pr", "update-branch", u, "--rebase"],
    "ready": lambda u, b: ["pr", "ready", u],
    "draft": lambda u, b: ["pr", "ready", u, "--undo"],
    "merge": lambda u, b: ["pr", "merge", u, f"--{config.get('merge_method')}"],
    "close": lambda u, b: ["pr", "close", u, *(["-c", b] if b else [])],
}


def act(op, url, body=""):
    if op == "comment" and not body:
        raise ValueError("A comment needs a body.")
    return gh(*ACTIONS[op](url, body)).strip() or "ok"
