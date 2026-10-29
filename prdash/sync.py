"""Background sync: refresh every view, turn changes into inbox events, push updates to open tabs."""
import queue, subprocess, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

from . import config, db, github, integrations

WAKE = threading.Event()  # set to sync now instead of waiting for the next tick
STATE = {"syncing": False, "at": None, "error": None, "took": None}
_subscribers, _sub_lock = [], threading.Lock()

REVIEW_VERB = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes on", "COMMENTED": "commented on",
               "DISMISSED": "dismissed their review of"}


# ---- server-sent events to open tabs

def subscribe():
    q = queue.Queue(maxsize=100)
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q):
    with _sub_lock:
        _subscribers.remove(q)


def broadcast(msg):
    with _sub_lock:
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:  # a stalled tab just misses updates; it refetches on reconnect
                pass


# ---- change detection

def label(pr):
    return f'{pr["repo"]}#{pr["number"]} {pr["title"]}'


def diff(old, new, me, review_before, review_after, local=None):
    """Inbox events between two snapshots. old/new: {url: pr}. Only things you'd act on: your PRs, PRs you follow,
    your review queue, and @mentions anywhere. local: {url: your fields}, for muted and followed PRs."""
    local = local or {}
    events = []
    for url in review_after - review_before:
        if url in new:
            kind, verb = ("urgent", "needs your review urgently") if new[url].get("urgent") else ("review_requested", "requested your review")
            events.append((url, kind, f'{new[url]["author"]} {verb}: {label(new[url])}'))
    for url, n in new.items():  # bumps on your PRs or on PRs waiting for your review
        b, o = n.get("bump"), old.get(url)
        if b and o and b != o.get("bump") and b["who"] != me and (n["author"] == me or url in review_after):
            events.append((url, "bump", f'{b["who"]} bumped {label(n)}'))
    for url, n in new.items():
        o = old.get(url)
        if not o:
            continue
        # PRs cached before prdash tracked mentions and new commits have no baseline to compare against
        m, was = (n.get("mentions") or {}).get(me.lower()), (o.get("mentions") or {}).get(me.lower())
        if "mentions" in o and m and m["who"] != me and (m["who"], m["at"]) != (was and (was["who"], was["at"])):
            events.append((url, "mention", f'{m["who"]} mentioned you on {label(n)}: "{m["text"]}"'))
        if "fixes_pushed" in o and n.get("fixes_pushed") and not o["fixes_pushed"] and n["author"] != me and \
                any(r["who"] == me and r["state"] == "CHANGES_REQUESTED" for r in n["reviewers"]):
            events.append((url, "pushed", f'{n["author"]} pushed new commits after your change request: {label(n)}'))
        if n["author"] != me and not local.get(url, {}).get("following"):
            continue
        if n["merged"] and not o["merged"]:
            events.append((url, "merged", f'Merged by {n["merged_by"]}: {label(n)}'))
            continue
        if n["state"] == "CLOSED" and o["state"] != "CLOSED":
            events.append((url, "closed", f"Closed: {label(n)}"))
        if n["ci"] != o["ci"] and n["ci"] in ("FAILURE", "ERROR"):
            events.append((url, "ci_failed", f"CI failed: {label(n)}"))
        elif n["ci"] == "SUCCESS" and o["ci"] in ("FAILURE", "ERROR", "PENDING"):
            events.append((url, "ci_passed", f"CI passed: {label(n)}"))
        before = {r["who"]: r["state"] for r in o["reviewers"]}
        for r in n["reviewers"]:
            if r["state"] != "REQUESTED" and before.get(r["who"]) != r["state"] and r["who"] != me:
                events.append((url, "review", f'{r["who"]} {REVIEW_VERB.get(r["state"], "reviewed")} {label(n)}'))
        if n["comments"] > o["comments"] and not any(e[0] == url and e[1] == "mention" for e in events):
            k, c = n["comments"] - o["comments"], n.get("last_comment")
            if not c:
                events.append((url, "comment", f'{k} new comment{"s" * (k > 1)} on {label(n)}'))
            elif k == 1 and c["who"] != me:
                events.append((url, "comment", f'{c["who"]} commented on {label(n)}: "{c["text"]}"'))
            elif k > 1:
                events.append((url, "comment", f'{k} new comments on {label(n)}, latest from {c["who"]}: "{c["text"]}"'))
        if n["mergeable"] == "CONFLICTING" and o["mergeable"] != "CONFLICTING":
            events.append((url, "conflict", f'Merge conflict with {n["base"]}: {label(n)}'))
    return [e for e in events if not local.get(e[0], {}).get("muted")]


# ---- flaky checks: a check that failed and then passed on the same commit flaked

def _rank(state):
    return 0 if state in github.FAILED else 1 if state in github.RUNNING else 3 if state == "SUCCESS" else 2

def record_checks(prs, checks):
    """checks: {url: [{name, state, run}]} for this sync's PRs. Returns the new flakes as [(url, repo, name)]."""
    seen = {(r, h, n): s for r, h, n, s in db.q("SELECT repo, head, name, state FROM check_state")}
    flakes, rows = [], []
    for url, cs in checks.items():
        p = prs[url]
        if not p.get("head"):
            continue
        repo = f'{p["owner"]}/{p["repo"]}'
        worst = {}  # a workflow on both push and pull_request runs twice under one name: keep the worse result
        for c in cs:
            if c["name"] not in worst or _rank(c["state"]) < _rank(worst[c["name"]]):
                worst[c["name"]] = c["state"]
        for name, state in worst.items():
            was = seen.get((repo, p["head"], name))
            if was in github.FAILED and state == "SUCCESS":
                flakes.append((url, repo, name))
            if was in github.FAILED and state in github.RUNNING:
                state = was  # a re-run in progress: remember it failed on this commit until it finishes
            rows.append((repo, p["head"], name, state))
    db.save_checks(rows, flakes)
    db.put("ci_repos", sorted(set(db.get("ci_repos", [])) | {r[0] for r in rows}))  # check_state only keeps current heads
    return flakes


def retries(prs, checks, me, local):
    """Retry until green: re-run failed GitHub Actions jobs on armed PRs until CI passes or attempts run out.
    Known-flaky failures on your own PRs arm it automatically when that setting is on."""
    events, cap, counts = [], max(1, min(10, config.get("retry_max"))), db.flake_counts()
    for url, p in prs.items():
        cs = checks.get(url)
        if cs is None or p["merged"] or p["state"] == "CLOSED":
            continue
        failed = [c for c in cs if c["state"] in (*github.FAILED, "CANCELLED")]
        if not failed and not local.get(url, {}).get("retry"):
            continue
        l = db.local_get(url)  # fresh: you may have pressed Retry or Stop since the sync started
        r = l.get("retry")
        if not r and failed and config.get("retry_flaky") and p["author"] == me and l.get("retry_done") != p["head"] \
                and all(counts.get((f'{p["owner"]}/{p["repo"]}', c["name"]), 0) >= 2 for c in failed):
            r = {"head": p["head"], "attempts": 0, "max": cap, "auto": True}
            events.append((url, "retrying", f"Retrying known-flaky checks: {label(p)}"))
        if not r:
            continue

        def stop(kind=None, text=None, gave_up=False):
            db.local_update(url, {"retry": None, **({"retry_done": p["head"]} if gave_up else {})})
            if kind:
                events.append((url, kind, text))

        if r["head"] != p["head"]:
            stop()  # new commits get a fresh CI run of their own
        elif any(c["state"] in github.RUNNING for c in cs) or time.time() - r.get("at", 0) < 90:
            continue  # still running, or the re-run hasn't registered on GitHub yet
        elif all(c["state"] in github.PASSED for c in cs):
            n = r["attempts"]
            stop("retry_ok", f'CI passed after {n} re-run{"s" * (n != 1)}: {label(p)}' if n else f"CI passed: {label(p)}")
        elif not failed:
            stop("retry_failed", f"Stopped retrying {label(p)}: a check needs attention on GitHub", gave_up=True)
        elif r["attempts"] >= r["max"]:
            names = ", ".join(sorted({c["name"] for c in failed})[:3])
            stop("retry_failed", f'Still failing after {r["attempts"]} re-runs ({names}): {label(p)}', gave_up=True)
        elif not github.failed_runs(failed):
            stop("retry_failed", f"Can't re-run {label(p)}: the failing checks aren't GitHub Actions", gave_up=True)
        else:
            try:
                github.rerun_runs(url, github.failed_runs(failed))
            except RuntimeError as e:
                stop("retry_failed", f"Re-run failed on {label(p)}: {str(e).splitlines()[0][:200]}", gave_up=True)
                continue
            if db.local_get(url).get("retry"):  # not stopped while the re-run request was in flight
                db.local_update(url, {"retry": {**r, "attempts": r["attempts"] + 1, "at": time.time()}})
    return events


def said(done):
    return (", ".join(done[:-1]) + " and " if len(done) > 1 else "") + done[-1]


def request_now(pr, r):
    """Mark a draft ready, request review from r's users and teams, then post to Slack if asked and not posted yet.
    On an error, what already happened stays done and the error says so."""
    done = []
    try:
        if pr["draft"]:
            github.act("ready", pr["url"])
            done.append("marked ready")
        done.append(github.request(pr, r["users"], r["teams"]))
        if r.get("slack") and not db.local_get(pr["url"]).get("posted"):
            done.append(integrations.slack_post(pr))
    except (RuntimeError, ValueError, OSError) as e:  # OSError: Slack unreachable
        raise RuntimeError((f"{said(done)}, then " if done else "") + f"failed: {str(e).splitlines()[0][:200]}") from None
    return said(done)


def requests_when_green(prs, checks, local):
    """Request review on armed PRs once CI passes. Waits across pushes; failures on the `skip` head (red when armed) don't count."""
    events = []
    for url, p in prs.items():
        if not local.get(url, {}).get("request_when_green"):
            continue
        l = db.local_get(url)  # fresh: you may have cancelled since the sync started
        r, cs = l.get("request_when_green"), checks.get(url) or []
        if not r:
            continue
        if p["merged"] or p["state"] == "CLOSED":
            db.local_update(url, {"request_when_green": None})
            continue
        # no checks yet: CI hasn't started, unless this repo has never had any
        if p["head"] == r.get("skip") or any(c["state"] in github.RUNNING for c in cs) or \
                (not cs and f'{p["owner"]}/{p["repo"]}' in db.get("ci_repos", [])):
            continue
        if not all(c["state"] in github.PASSED for c in cs):
            if not l.get("retry"):  # Retry until green gets its chance first
                db.local_update(url, {"request_when_green": None})
                events.append((url, "request_failed", f"CI failed on {label(p)}; didn't request review"))
            continue
        db.local_update(url, {"request_when_green": None})
        try:
            done = request_now(p, r)
            events.append((url, "requested", f"{done[0].upper()}{done[1:]}: {label(p)}"))
        except RuntimeError as e:
            events.append((url, "request_failed", f"Review request on {label(p)}: {e}"))
    return events


def notify(events):
    template = config.get("notify_cmd")
    for _, _, text in events if template else []:
        try:
            subprocess.Popen(config.expand(template, text=text), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            break  # misconfigured command; the inbox still has everything


def wake_snoozes(prs):
    now = time.strftime("%Y-%m-%d")
    events = []
    for url, local in db.local_all().items():
        if local.get("snooze") and local["snooze"] <= now:
            db.local_update(url, {"snooze": None})
            if url in prs:
                events.append((url, "snooze", f"Back from snooze: {label(prs[url])}"))
    return events


def on_merge_jira(events, prs):
    target = config.get("jira_on_merge").strip()
    if not target or not integrations.jira_ready():
        return []
    out = []
    for url, kind, _ in events:
        if kind != "merged":
            continue
        for key in prs[url]["jira"]:
            try:
                integrations.jira_transition(key, target)
                out.append((url, "jira", f"Moved {key} to {target}"))
            except (RuntimeError, ValueError) as e:
                out.append((url, "jira_error", str(e)))
    return out


# ---- the sync itself

def sync_once():
    started = time.time()
    me = github.viewer()
    views = ["mine", "review"] + (["team"] if config.get("team_view") else [])
    with ThreadPoolExecutor(len(views)) as ex:
        found = dict(zip(views, ex.map(github.membership, views)))
    old, old_members = db.all_prs()
    old_views = {v: set(urls) for v, urls in old_members.items()}
    # open PRs refetch every run (CI and mergeability change without bumping updatedAt); merged ones only once
    ids = {i: u for opened, merged in found.values() for i, u in opened}
    ids.update({i: u for _, merged in found.values() for i, u in merged if u not in old or not old[u].get("merged")})
    new = github.fetch(list(ids))
    checks = {u: p.pop("_checks", []) for u, p in new.items()}
    keep = {u: old[u] for _, merged in found.values() for _, u in merged if u not in new and u in old}
    members = {v: [u for _, u in opened + merged if u in new or u in keep] for v, (opened, merged) in found.items()}
    first = not db.get("synced_once")
    local = db.local_all()
    events = [] if first else diff(old, new, me, old_views.get("review", set()), set(members.get("review", [])), local)
    db.save_views(members, {**keep, **new})
    record_checks(new, checks)
    events += retries(new, checks, me, local)
    events += requests_when_green(new, checks, local)
    if not config.get("team_view"):
        db.save_views({"team": []}, {})
    prs = {**keep, **new}
    events += wake_snoozes(prs) + on_merge_jira(events, prs)
    jira_changed = _refresh_jira(prs, members)
    db.put("synced_once", True)
    if events:
        db.add_events(events)
        notify(events)
    STATE.update(at=time.time(), took=round(time.time() - started, 1), error=None)
    # open tabs only refetch and re-render when something they show actually moved
    changed = (jira_changed or bool(events) or any(old.get(u) != p for u, p in new.items())
               or any(set(urls) != old_views.get(v, set()) for v, urls in members.items()))
    return events, changed


def _refresh_jira(prs, members):
    """Refresh ticket status for your PRs and review queue; True if any status or assignee changed."""
    keys = {k for v in ("mine", "review") for u in members.get(v, []) for k in prs[u]["jira"]}
    shown = lambda: {k: (v.get("status"), v.get("assignee")) for k, v in db.get("jira_cache", {}).items()}
    before = shown()
    try:
        integrations.jira_issues(keys)
    except (RuntimeError, ValueError, OSError) as e:
        STATE["jira_error"] = str(e)
    return shown() != before


def loop():
    while True:
        WAKE.clear()  # a sync requested before this run starts is covered by it
        STATE["syncing"] = True
        broadcast({"type": "syncing"})
        events, changed = [], True
        try:
            events, changed = sync_once()
        except Exception as e:  # keep looping; GitHub hiccups are common
            STATE["error"] = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
            traceback.print_exc()
        STATE["syncing"] = False
        broadcast({"type": "sync", "changed": changed, "events": [{"url": u, "kind": k, "text": t} for u, k, t in events], **STATE})
        WAKE.wait(config.get("poll_seconds"))


def start():
    threading.Thread(target=loop, daemon=True, name="prdash-sync").start()


def now():
    """Sync as soon as possible (the loop picks it up between runs)."""
    WAKE.set()


def snapshot():
    prs, views = db.all_prs()
    return {"prs": prs, "views": views, "local": db.local_all(), "reviews": db.reviews(), "jira": db.get("jira_cache", {}),
            "me": db.get("viewer"), "unread": db.q("SELECT COUNT(*) FROM events WHERE read = 0")[0][0], **STATE}
