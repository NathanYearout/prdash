"""SQLite storage: cached PRs, view membership, your local notes, inbox events, Claude reviews, settings.

One connection shared by all threads behind a lock; sqlite is plenty fast for a few hundred PRs.
"""
import json, os, sqlite3, threading, time
from contextlib import contextmanager
from pathlib import Path

HOME = Path(os.environ.get("PRDASH_HOME", Path.home() / ".config/prdash")).expanduser()
LOCK = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (url TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS view_prs (view TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY (view, url));
CREATE TABLE IF NOT EXISTS local (url TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, url TEXT, kind TEXT NOT NULL,
                                   text TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reviews (url TEXT PRIMARY KEY, status TEXT, started REAL, finished REAL, output TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS check_state (repo TEXT NOT NULL, head TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
                                        PRIMARY KEY (repo, head, name));
CREATE TABLE IF NOT EXISTS flakes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, repo TEXT NOT NULL, name TEXT NOT NULL,
                                   url TEXT NOT NULL);
"""


def conn():
    global _conn
    with LOCK:
        if _conn is None:
            HOME.mkdir(parents=True, exist_ok=True)
            os.chmod(HOME, 0o700)  # the db and its WAL files hold API tokens
            _conn = sqlite3.connect(HOME / "prdash.db", check_same_thread=False, isolation_level=None)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _migrate_json()
        return _conn


def q(sql, *args):
    with LOCK:
        return conn().execute(sql, args).fetchall()


@contextmanager
def tx():
    with LOCK:
        c = conn()
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


# ---- kv: internal state and settings, JSON-encoded

def get(key, default=None):
    rows = q("SELECT value FROM kv WHERE key = ?", key)
    return json.loads(rows[0][0]) if rows else default


def put(key, value):
    if value is None:
        q("DELETE FROM kv WHERE key = ?", key)
    else:
        q("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, json.dumps(value))


# ---- PRs and views

def save_views(members, prs):
    """members: {view: [urls]} for the views just synced; prs: {url: pr}. Drops PRs no view references."""
    with tx() as c:
        c.executemany("INSERT INTO prs (url, data) VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET data = excluded.data",
                      [(u, json.dumps(p)) for u, p in prs.items()])
        for view, urls in members.items():
            c.execute("DELETE FROM view_prs WHERE view = ?", (view,))
            c.executemany("INSERT INTO view_prs (view, url) VALUES (?, ?)", [(view, u) for u in urls])
        c.execute("DELETE FROM prs WHERE url NOT IN (SELECT url FROM view_prs)")


def all_prs():
    """({url: pr}, {view: [urls]})"""
    prs = {u: json.loads(d) for u, d in q("SELECT url, data FROM prs")}
    views = {}
    for view, url in q("SELECT view, url FROM view_prs"):
        views.setdefault(view, []).append(url)
    return prs, views


def get_prs(urls):
    if not urls:
        return {}
    marks = ",".join("?" * len(urls))
    return {u: json.loads(d) for u, d in q(f"SELECT url, data FROM prs WHERE url IN ({marks})", *urls)}


# ---- local notes

def local_all():
    return {u: json.loads(d) for u, d in q("SELECT url, data FROM local")}


def local_get(url):
    rows = q("SELECT data FROM local WHERE url = ?", url)
    return json.loads(rows[0][0]) if rows else {}


def local_update(url, fields):
    """Merge fields; a None value deletes that field."""
    with tx() as c:
        rows = c.execute("SELECT data FROM local WHERE url = ?", (url,)).fetchall()
        data = {**(json.loads(rows[0][0]) if rows else {}), **fields}
        data = {k: v for k, v in data.items() if v is not None}
        c.execute("INSERT INTO local (url, data) VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET data = excluded.data",
                  (url, json.dumps(data)))
    return data


# ---- inbox

def add_events(events):
    """events: [(url, kind, text)] -> number added"""
    now = time.time()
    with tx() as c:
        c.executemany("INSERT INTO events (ts, url, kind, text) VALUES (?, ?, ?, ?)", [(now, *e) for e in events])
    return len(events)


def events(limit=200):
    rows = q("SELECT id, ts, url, kind, text, read FROM events ORDER BY id DESC LIMIT ?", limit)
    return [dict(zip(("id", "ts", "url", "kind", "text", "read"), r)) for r in rows]


def delete_events(ids=None):
    """Delete these events, or every read one when ids is None."""
    if ids is None:
        q("DELETE FROM events WHERE read = 1")
    else:
        with tx() as c:
            c.executemany("DELETE FROM events WHERE id = ?", [(i,) for i in ids])


def mark_read(ids=None):
    if ids is None:
        q("UPDATE events SET read = 1 WHERE read = 0")
    else:
        with tx() as c:
            c.executemany("UPDATE events SET read = 1 WHERE id = ?", [(i,) for i in ids])
    q("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 1000")  # keep the inbox bounded


# ---- CI checks, for spotting flaky ones

def save_checks(rows, flakes):
    """rows: every check's current state as (repo, head, name, state); flakes: [(url, repo, name)] seen this sync.
    Only the commits in rows are kept: once a PR moves on, its old commit can't flake again."""
    now = time.time()
    with tx() as c:
        c.execute("DELETE FROM check_state")
        c.executemany("INSERT OR REPLACE INTO check_state (repo, head, name, state) VALUES (?, ?, ?, ?)", rows)
        c.executemany("INSERT INTO flakes (ts, repo, name, url) VALUES (?, ?, ?, ?)", [(now, r, n, u) for u, r, n in flakes])
        c.execute("DELETE FROM flakes WHERE ts < ?", (now - 90 * 86400,))


def flake_counts(days=30):
    """{(repo, check name): flakes in the last `days`}"""
    rows = q("SELECT repo, name, COUNT(*) FROM flakes WHERE ts > ? GROUP BY repo, name", time.time() - days * 86400)
    return {(r, n): k for r, n, k in rows}


def flakes(days=30):
    rows = q("SELECT ts, repo, name, url FROM flakes WHERE ts > ? ORDER BY ts DESC", time.time() - days * 86400)
    return [dict(zip(("ts", "repo", "name", "url"), r)) for r in rows]


# ---- Claude reviews

def review_set(url, **fields):
    with tx() as c:
        c.execute("INSERT INTO reviews (url) VALUES (?) ON CONFLICT(url) DO NOTHING", (url,))
        for k, v in fields.items():
            assert k in ("status", "started", "finished", "output"), k
            c.execute(f"UPDATE reviews SET {k} = ? WHERE url = ?", (v, url))


def reviews():
    rows = q("SELECT url, status, started, finished, output FROM reviews")
    return {r[0]: dict(zip(("status", "started", "finished", "output"), r[1:])) for r in rows}


def _migrate_json():
    """One-time import of the pre-SQLite notes file."""
    old = HOME / "local.json"
    if old.exists() and not _conn.execute("SELECT 1 FROM local LIMIT 1").fetchall():
        for url, data in json.loads(old.read_text() or "{}").items():
            if data:
                _conn.execute("INSERT INTO local (url, data) VALUES (?, ?)", (url, json.dumps(data)))
        old.rename(old.with_suffix(".json.migrated"))
