"""Slack and Jira over plain HTTPS. Both are optional and configured on the settings page."""
import base64, json, os, re, time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import config, db

SLACK_API = os.environ.get("PRDASH_SLACK_API", "https://slack.com/api")  # overridable for tests


def _request(url, headers, data=None, method=None):
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as r:
            body = r.read()
    except HTTPError as e:
        raise RuntimeError(f"{e.code} from {url.split('?')[0]}: {e.read()[:200].decode(errors='replace')}") from None
    return json.loads(body) if body else {}


# ---- Slack

def slack_ready():
    return bool(config.get("slack_token"))


def slack(method, **params):
    token = config.get("slack_token")
    if not token:
        raise ValueError("Add a Slack token in Settings first.")
    out = _request(f"{SLACK_API}/{method}", {"Authorization": f"Bearer {token}"},
                   urlencode({k: v for k, v in params.items() if v is not None}).encode())
    if not out.get("ok"):
        raise RuntimeError(f"Slack {method}: {out.get('error', 'failed')}")
    return out


def announcement(pr):
    """The team-style post: 'PR to <title, lowercased, ticket prefix dropped>: <url>'."""
    title = re.sub(r"^\s*(\[[A-Z][A-Z0-9]+-\d+\]|[A-Z][A-Z0-9]+-\d+:)\s*", "", pr["title"])
    if re.match(r"^[A-Z][a-z]", title):
        title = title[0].lower() + title[1:]
    text = config.get("slack_template")
    for k, v in {"title": title, "raw_title": pr["title"], "url": pr["url"], "repo": pr["repo"], "number": pr["number"]}.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def slack_post(pr):
    channel = config.get("slack_channel")
    if not channel:
        raise ValueError("Set a Slack channel in Settings first.")
    out = slack("chat.postMessage", channel=channel, text=announcement(pr), unfurl_links="false")
    link = slack("chat.getPermalink", channel=out["channel"], message_ts=out["ts"]).get("permalink")
    db.local_update(pr["url"], {"posted": _now_iso(), "slack": {"channel": out["channel"], "ts": out["ts"], "link": link}})
    return f"posted to {channel}"


def slack_find(pr):
    """Look for an existing announcement of this PR, so manual posts count too."""
    channel = config.get("slack_channel").lstrip("#")
    out = slack("search.messages", query=f'"{pr["url"]}"' + (f" in:#{channel}" if channel and not channel.startswith(("C", "G")) else ""),
                count=5, sort="timestamp", sort_dir="asc")
    matches = out.get("messages", {}).get("matches", [])
    if not matches:
        return None
    m = matches[0]
    ts = m.get("ts")
    # a match inside a thread: the thread root is what the team replies under
    thread = re.search(r"thread_ts=([\d.]+)", m.get("permalink", ""))
    info = {"channel": m["channel"]["id"], "ts": thread.group(1) if thread else ts, "link": m.get("permalink")}
    db.local_update(pr["url"], {"posted": db.local_get(pr["url"]).get("posted") or _now_iso(), "slack": info})
    return info


def slack_user(uid):
    cached = db.get("slack_user:" + uid)
    if cached is None:
        try:
            u = slack("users.info", user=uid)["user"]
            cached = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name") or uid
        except RuntimeError:
            cached = uid
        db.put("slack_user:" + uid, cached)
    return cached


def slack_thread(pr):
    """Announcement thread: parent reactions and replies, oldest first."""
    info = db.local_get(pr["url"]).get("slack") or slack_find(pr)
    if not info:
        return None
    msgs = slack("conversations.replies", channel=info["channel"], ts=info["ts"], limit=50).get("messages", [])
    if not msgs:
        return {"link": info.get("link"), "reactions": [], "replies": []}
    mention = lambda text: re.sub(r"<@(\w+)(\|[^>]*)?>", lambda m: "@" + slack_user(m.group(1)), text or "")
    return {
        "link": info.get("link"),
        "reactions": [{"name": r["name"], "count": r["count"]} for r in msgs[0].get("reactions", [])],
        "replies": [{"who": slack_user(m.get("user", "?")), "text": mention(m.get("text")), "ts": float(m["ts"])} for m in msgs[1:]],
    }


# ---- Jira

def jira_ready():
    return all(config.get(k) for k in ("jira_url", "jira_email", "jira_token"))


def jira(path, body=None, method=None):
    auth = base64.b64encode(f'{config.get("jira_email")}:{config.get("jira_token")}'.encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/json"}
    return _request(config.get("jira_url").rstrip("/") + path, headers, json.dumps(body).encode() if body else None, method)


def jira_issues(keys):
    """{key: {status, category, assignee, summary}}, cached 5 minutes per key."""
    if not keys or not jira_ready():
        return {}
    cache = db.get("jira_cache", {})
    fresh = time.time() - 300
    stale = sorted(k for k in keys if cache.get(k, {}).get("at", 0) < fresh)
    for i in range(0, len(stale), 50):
        chunk = stale[i:i + 50]
        jql = f"key in ({','.join(chunk)})"
        out = jira(f"/rest/api/3/search/jql?jql={quote(jql)}&fields=status,assignee,summary&maxResults=50")
        for issue in out.get("issues", []):
            f = issue["fields"]
            cache[issue["key"]] = {"status": f["status"]["name"], "category": f["status"]["statusCategory"]["key"],
                                   "assignee": (f.get("assignee") or {}).get("displayName"), "summary": f.get("summary"),
                                   "at": time.time()}
        for k in chunk:  # unknown keys (e.g. CVE-2024-1) shouldn't be re-queried every sync
            cache.setdefault(k, {"missing": True, "at": time.time()})
    db.put("jira_cache", cache)
    return {k: cache[k] for k in keys if k in cache and not cache[k].get("missing")}


def jira_transition(key, to_status):
    transitions = jira(f"/rest/api/3/issue/{key}/transitions")["transitions"]
    match = next((t for t in transitions if to_status.lower() in (t["name"].lower(), t["to"]["name"].lower())), None)
    if not match:
        raise RuntimeError(f"{key}: no transition to '{to_status}' (have: {', '.join(t['to']['name'] for t in transitions)})")
    jira(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}}, "POST")
    cache = db.get("jira_cache", {})
    cache.pop(key, None)
    db.put("jira_cache", cache)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
