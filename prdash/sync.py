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


def diff(old, new, me, review_before, review_after):
    """Inbox events between two snapshots. old/new: {url: pr}. Only things you'd act on."""
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
        if not o or n["author"] != me:
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
        if n["comments"] > o["comments"]:
            k, c = n["comments"] - o["comments"], n.get("last_comment")
            if not c:
                events.append((url, "comment", f'{k} new comment{"s" * (k > 1)} on {label(n)}'))
            elif k == 1 and c["who"] != me:
                events.append((url, "comment", f'{c["who"]} commented on {label(n)}: "{c["text"]}"'))
            elif k > 1:
                events.append((url, "comment", f'{k} new comments on {label(n)}, latest from {c["who"]}: "{c["text"]}"'))
        if n["mergeable"] == "CONFLICTING" and o["mergeable"] != "CONFLICTING":
            events.append((url, "conflict", f'Merge conflict with {n["base"]}: {label(n)}'))
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
    keep = {u: old[u] for _, merged in found.values() for _, u in merged if u not in new and u in old}
    members = {v: [u for _, u in opened + merged if u in new or u in keep] for v, (opened, merged) in found.items()}
    first = not db.get("synced_once")
    events = [] if first else diff(old, new, me, old_views.get("review", set()), set(members.get("review", [])))
    db.save_views(members, {**keep, **new})
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
