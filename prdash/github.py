"""GitHub via the `gh` CLI: which PRs belong to each view, their details, CI checks, and PR actions."""
import json, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import config, db

PR_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
BUMP = re.compile(r"\bbump(ed|ing)?\b", re.I)
URGENT = re.compile(r"\burgent\b", re.I)
VIEWS = {"mine": "author:@me", "review": "review-requested:@me", "team": ""}
MENTION = re.compile(r"(?<![\w/])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")
FAILED = ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE")
RUNNING = ("IN_PROGRESS", "QUEUED", "PENDING", "EXPECTED", "WAITING", "REQUESTED")
PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")

PR_FIELDS = """
  id url number title isDraft state createdAt updatedAt mergedAt closedAt additions deletions changedFiles
  headRefName baseRefName mergeable mergeStateStatus reviewDecision isCrossRepository
  autoMergeRequest { mergeMethod enabledBy { login } } viewerCanEnableAutoMerge viewerCanDisableAutoMerge
  author { login } mergedBy { login }
  repository { name owner { login } }
  labels(first: 10) { nodes { name } }
  comments(last: 10) { totalCount nodes { body createdAt author { login } } }
  reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { slug } } } }
  latestReviews(first: 15) { nodes { author { login } state submittedAt commit { oid } } }
  latestOpinionatedReviews(first: 15) { nodes { author { login } commit { oid } } }
  reviews(first: 1) { nodes { submittedAt } }
  reviewThreads(first: 100) { nodes { isResolved } }
  commits(last: 1) { nodes { commit { oid committedDate statusCheckRollup { state contexts(first: 60) { nodes { __typename
    ... on CheckRun { name status conclusion checkSuite { workflowRun { databaseId workflow { name } } } }
    ... on StatusContext { context state } } } } } } }
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
    # GitHub keeps CHANGES_REQUESTED until the reviewer reviews again; new commits after it hand the PR back to them
    changes = p["review"] == "CHANGES_REQUESTED" and not p.get("fixes_pushed")
    if changes or p["ci"] in ("FAILURE", "ERROR") or p["mergeable"] == "CONFLICTING":
        return "needs_work"
    if p["review"] == "APPROVED":
        return "approved"
    return "in_review"


def blocker(p):
    """Why an open, ready PR can't merge yet, from GitHub's merge state. None when nothing new to say (conflicts are already Needs work)."""
    if p["merged"] or p["draft"] or p["state"] != "OPEN" or p.get("merge_state") not in ("BEHIND", "BLOCKED"):
        return None
    if p["merge_state"] == "BEHIND":
        return f'Behind {p["base"]}'
    if p["review"] in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        return "Needs approval"
    return "Required checks" if p["ci"] != "SUCCESS" else "Blocked by branch rules"


def bump(comments):
    """The latest short "bump" comment ("bump", "friendly bump 🙏", "bumping this"), as {who, at}."""
    hits = [c for c in comments if c and len((c["body"] or "").strip()) <= 40 and BUMP.search(c["body"] or "")]
    return {"who": (hits[-1]["author"] or {}).get("login", "ghost"), "at": hits[-1]["createdAt"]} if hits else None


def plain(body, limit=None):
    """Comment text without the HTML comments PR templates leave behind; one line when trimmed for a notification."""
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"<!--.*?-->", "", body or "", flags=re.S)).strip()
    if limit:
        text = " ".join(text.split())
        text = text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return text


def last_comment(comments):
    c = comments[-1] if comments else None
    return {"who": (c["author"] or {}).get("login", "ghost"), "at": c["createdAt"], "text": plain(c["body"], 140)} if c else None


def contexts(rollup):
    """Each check on a commit as {name, state, url, run}; run is the Actions run id when it can be re-run."""
    out = []
    for c in (rollup["contexts"]["nodes"] if rollup else []):
        if c["__typename"] == "CheckRun":
            run = (c["checkSuite"] or {}).get("workflowRun") or {}
            name = f'{run["workflow"]["name"]} / {c["name"]}' if run.get("workflow") else c["name"]
            out.append({"name": name, "state": (c["conclusion"] or c["status"] or "").upper(), "url": safe_url(c.get("detailsUrl")),
                        "run": run.get("databaseId")})
        else:
            out.append({"name": c["context"], "state": c["state"], "url": safe_url(c.get("targetUrl")), "run": None})
    return out


def mentions(comments):
    """{login: {who, at, text}}: the latest comment that @mentions each person."""
    out = {}
    for c in comments:
        for login in set(MENTION.findall(c["body"] or "")):
            out[login.lower()] = {"who": (c["author"] or {}).get("login", "ghost"), "at": c["createdAt"], "text": plain(c["body"], 140)}
    return out


def flatten(n):
    commits = n["commits"]["nodes"]
    head = commits[0]["commit"] if commits else {}
    rollup = head.get("statusCheckRollup")
    reviewed = {r["author"]["login"]: r["state"] for r in n["latestReviews"]["nodes"] if r["author"]}
    # the commit each person last reviewed: their latest approval or change request, else their latest comment review
    # (a thread reply is a comment review of its own, so latestReviews alone would move past a real review)
    at = {r["author"]["login"]: (r.get("commit") or {}).get("oid")
          for r in n["latestReviews"]["nodes"] + n["latestOpinionatedReviews"]["nodes"] if r["author"]}
    asked = [r["submittedAt"] for r in n["latestReviews"]["nodes"] if r["state"] == "CHANGES_REQUESTED" and r["submittedAt"]]
    requested = [rr["requestedReviewer"] for rr in n["reviewRequests"]["nodes"] if rr["requestedReviewer"]]
    teams = {r["slug"] for r in requested if r.get("slug")}
    requested = [r.get("login") or r.get("slug") for r in requested]
    am = n.get("autoMergeRequest")
    p = {
        "id": n["id"], "url": n["url"], "number": n["number"], "title": n["title"], "draft": n["isDraft"],
        "state": n["state"], "repo": n["repository"]["name"], "owner": n["repository"]["owner"]["login"],
        "author": (n["author"] or {}).get("login", "ghost"),
        "created": n["createdAt"], "updated": n["updatedAt"], "closed": n["closedAt"],
        "merged": n["mergedAt"], "merged_by": (n["mergedBy"] or {}).get("login"),
        "adds": n["additions"], "dels": n["deletions"], "files": n["changedFiles"],
        "branch": n["headRefName"], "base": n["baseRefName"], "mergeable": n["mergeable"], "fork": n.get("isCrossRepository", False),
        "merge_state": n.get("mergeStateStatus"), "can_automerge": n.get("viewerCanEnableAutoMerge", False),
        "can_automerge_off": n.get("viewerCanDisableAutoMerge", False),
        "auto_merge": am and {"method": am["mergeMethod"].lower(), "by": (am["enabledBy"] or {}).get("login", "ghost")},
        "review": n["reviewDecision"], "ci": rollup["state"] if rollup else None,
        "labels": [l["name"] for l in n["labels"]["nodes"]],
        "comments": n["comments"]["totalCount"],
        "bump": bump(n["comments"]["nodes"]),
        "last_comment": last_comment(n["comments"]["nodes"]),
        "urgent": bool(URGENT.search(n["title"])) or any(URGENT.search(l["name"]) for l in n["labels"]["nodes"]),
        "jira": sorted(set(JIRA_KEY.findall(n["title"] + " " + n["headRefName"]))),
        "reviewers": [{"who": k, "state": v, "commit": at.get(k)} for k, v in reviewed.items()]
        + [{"who": r, "state": "REQUESTED", **({"team": True} if r in teams else {})} for r in requested if r not in reviewed],
        "requested": requested,
        "head": head.get("oid"), "pushed": head.get("committedDate"),
        "fixes_pushed": bool(asked and head.get("committedDate") and head["committedDate"] > max(asked)),
        "first_review": next((r["submittedAt"] for r in n["reviews"]["nodes"]), None),
        "open_threads": sum(not t["isResolved"] for t in n["reviewThreads"]["nodes"]),
        "mentions": mentions(n["comments"]["nodes"]),
        "_checks": [{"name": c["name"], "state": c["state"], "run": c["run"]} for c in contexts(rollup)],
    }
    p["status"], p["blocker"] = status(p), blocker(p)
    return p


def search_ids(q):
    """[(id, url)] for every PR matching a search, 100 per page."""
    out, cursor = [], None
    while True:
        s = gql("""query($q: String!, $c: String) { search(query: $q, type: ISSUE, first: 100, after: $c) {
                     pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { id url } } } }""", q=q, c=cursor)["search"]
        out += [(n["id"], n["url"]) for n in s["nodes"] if n]
        # GitHub search returns at most 1000 results; narrow big orgs with the extra_query setting
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

    def page(c):
        try:
            return gql(query, ids=c)["nodes"]
        except RuntimeError as e:  # GitHub times out a heavy batch now and then (502/504): halve it and try again
            if len(c) > 1 and any(code in str(e) for code in ("HTTP 502", "HTTP 504", "timeout", "timed out")):
                return page(c[:len(c) // 2]) + page(c[len(c) // 2:])
            raise
    with ThreadPoolExecutor(8) as ex:
        pages = list(ex.map(page, chunks))
    return {p["url"]: p for p in (flatten(n) for page in pages for n in page if n)}


def safe_url(u):
    """CI providers set these links; anything but http(s) (e.g. javascript:) is dropped."""
    return u if isinstance(u, str) and u.lower().startswith(("https://", "http://")) else None


def checks(url):
    """CI checks for the head commit: name, state, link, and the Actions run id when re-runnable."""
    return checks_at(url)[1]


def checks_at(url):
    """(head commit oid, its checks), read together so they can't disagree after a push."""
    q = """query($u: URI!) { resource(url: $u) { ... on PullRequest { commits(last: 1) { nodes { commit { oid
             statusCheckRollup { contexts(first: 100) { nodes { __typename
               ... on CheckRun { name status conclusion detailsUrl checkSuite { workflowRun { databaseId workflow { name } } } }
               ... on StatusContext { context state targetUrl } } } } } } } } } }"""
    commits = gql(q, u=url)["resource"]["commits"]["nodes"]
    head = commits[0]["commit"] if commits else {}
    out = contexts(head.get("statusCheckRollup"))
    rank = {"FAILURE": 0, "ERROR": 0, "TIMED_OUT": 0, "CANCELLED": 1, "ACTION_REQUIRED": 1, "IN_PROGRESS": 2,
            "QUEUED": 2, "PENDING": 2, "EXPECTED": 2}
    return head.get("oid"), sorted(out, key=lambda c: (rank.get(c["state"], 3), c["name"]))


REVIEW_WHAT = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes", "COMMENTED": "reviewed", "DISMISSED": "review dismissed"}


def conversation(url):
    """Description, comments, reviews, and inline code comments, oldest first."""
    who = lambda n: (n["author"] or {}).get("login", "ghost")
    q = """query($u: URI!) { resource(url: $u) { ... on PullRequest { body createdAt author { login }
             comments(last: 100) { nodes { body createdAt url author { login } } }
             reviews(last: 50) { nodes { state body submittedAt url author { login } } }
             reviewThreads(first: 100) { nodes { id isResolved isOutdated path line originalLine
               comments(first: 50) { nodes { body createdAt url author { login } } } } } } } }"""
    pr = gql(q, u=url)["resource"]
    out = [{"who": who(pr), "what": "opened", "at": pr["createdAt"], "text": plain(pr["body"]), "url": url}] if plain(pr["body"]) else []
    out += [{"who": who(c), "what": "commented", "at": c["createdAt"], "text": plain(c["body"]), "url": c["url"]}
            for c in pr["comments"]["nodes"]]
    out += [{"who": who(r), "what": REVIEW_WHAT.get(r["state"], "reviewed"), "at": r["submittedAt"], "text": plain(r["body"]), "url": r["url"]}
            for r in pr["reviews"]["nodes"]  # bare COMMENTED reviews only hold inline comments, which come from the threads
            if plain(r["body"]) or r["state"] in ("APPROVED", "CHANGES_REQUESTED")]
    for t in pr["reviewThreads"]["nodes"]:
        cs = [{"who": who(c), "at": c["createdAt"], "text": plain(c["body"]), "url": c["url"]} for c in t["comments"]["nodes"]]
        if cs:
            out.append({"who": cs[0]["who"], "what": "thread", "at": cs[0]["at"], "url": cs[0]["url"], "id": t["id"],
                        "resolved": t["isResolved"], "outdated": t["isOutdated"], "path": t["path"],
                        "line": t["line"] or t["originalLine"], "comments": cs})
    return sorted(out, key=lambda c: c["at"] or "")


def failed_runs(checks_):
    return {c["run"] for c in checks_ if c["run"] and c["state"] in (*FAILED, "CANCELLED")}


def rerun_runs(url, runs):
    repo = "/".join(url.split("/")[3:5])
    for run in sorted(runs):
        gh("run", "rerun", str(run), "--failed", "-R", repo)


def rerun_failed(url):
    failed = failed_runs(checks(url))
    if not failed:
        raise ValueError("No failed GitHub Actions runs to re-run (other CI systems re-run from their own UI).")
    rerun_runs(url, failed)
    return f"re-running {len(failed)} workflow run(s)"


def request(pr, users, teams=()):
    """Ask people and teams (slugs) to review. GitHub re-notifies someone who already reviewed; a team request stays as it is."""
    gh("api", "-X", "POST", f'repos/{pr["owner"]}/{pr["repo"]}/pulls/{pr["number"]}/requested_reviewers',
       *[a for l in users for a in ("-f", f"reviewers[]={l}")], *[a for t in teams for a in ("-f", f"team_reviewers[]={t}")])
    return "requested review from " + ", ".join([*teams, *users])


def rerequest(pr, logins):
    known = {r["who"] for r in pr["reviewers"]}
    logins = [l for l in logins if l in known]
    if not logins:
        raise ValueError("Pick someone who's already a reviewer on this PR.")
    request(pr, logins)
    return "re-requested review from " + ", ".join(logins)


def _daily(key, fetch):
    hit = db.get(key)
    if not hit or hit["at"] < time.time() - 86400:
        hit = {"at": time.time(), "v": fetch()}
        db.put(key, hit)
    return hit["v"]


def candidates(pr):
    """Who else GitHub can ask to review: {teams, people}. An org's teams, or the collaborators on a user's repo. Cached a day."""
    o, repo = pr["owner"], pr["repo"]

    def teams():
        try:
            return gh("api", f"orgs/{o}/teams", "--paginate", "--jq", ".[].slug").split()
        except RuntimeError as e:
            if "403" in str(e):
                return []  # an org that hides its teams from you
            if "404" not in str(e):
                raise
            return None  # a user, not an org

    def people():
        try:
            return gh("api", f"repos/{o}/{repo}/collaborators", "--paginate", "--jq", ".[].login").split()
        except RuntimeError:  # listing them needs push access; the page still offers the people it knows
            return []
    t = _daily(f"teams:{o}", teams)
    return {"teams": t, "people": []} if t is not None else {"teams": [], "people": _daily(f"collaborators:{o}/{repo}", people)}


def _thread_of(url, thread):
    t = gql("query($id: ID!) { node(id: $id) { ... on PullRequestReviewThread { pullRequest { url } } } }", id=thread)["node"]
    if not t or t["pullRequest"]["url"] != url:
        raise ValueError("That comment thread isn't on this PR.")


def thread_reply(url, thread, body):
    if not body.strip():
        raise ValueError("A reply needs a body.")
    _thread_of(url, thread)
    gql("mutation($id: ID!, $b: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $b}) { comment { id } } }",
        id=thread, b=body)
    return "replied"


def thread_resolve(url, thread, resolved=True):
    _thread_of(url, thread)
    op = "resolveReviewThread" if resolved else "unresolveReviewThread"
    gql(f"mutation($id: ID!) {{ {op}(input: {{threadId: $id}}) {{ thread {{ isResolved }} }} }}", id=thread)
    return "resolved" if resolved else "reopened"


# op -> gh args. The comment/review body is appended where an op takes one.
ACTIONS = {
    "approve": lambda u, b: ["pr", "review", u, "--approve", *(["-b", b] if b else [])],
    "request_changes": lambda u, b: ["pr", "review", u, "--request-changes", "-b", b or "See comments."],
    "comment": lambda u, b: ["pr", "comment", u, "-b", b],
    "rebase": lambda u, b: ["pr", "update-branch", u, "--rebase"],
    "ready": lambda u, b: ["pr", "ready", u],
    "draft": lambda u, b: ["pr", "ready", u, "--undo"],
    "merge": lambda u, b: ["pr", "merge", u, f"--{config.get('merge_method')}"],
    "automerge": lambda u, b: ["pr", "merge", u, "--auto", f"--{config.get('merge_method')}"],
    "automerge_off": lambda u, b: ["pr", "merge", u, "--disable-auto"],
    "close": lambda u, b: ["pr", "close", u, *(["-c", b] if b else [])],
}


def act(op, url, body=""):
    if op == "comment" and not body:
        raise ValueError("A comment needs a body.")
    return gh(*ACTIONS[op](url, body)).strip() or "ok"
