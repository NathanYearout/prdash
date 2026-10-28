"""Keep prdash current. A git checkout fast-forwards on start (never over local changes or a diverged branch);
a pipx install upgrades in the background at most once a day and applies on the next start."""
import os, shutil, subprocess, sys, threading, time
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent


def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=20)


def from_git():
    """Fast-forward a clean checkout to its upstream. Returns a short message when it updated, else None."""
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        return None  # local changes: this is someone's working copy, leave it alone
    before = _git("rev-parse", "HEAD").stdout.strip()
    if _git("pull", "--ff-only", "--quiet").returncode != 0:
        return None  # offline, no upstream, or diverged history
    after = _git("rev-parse", "HEAD").stdout.strip()
    return f"updated {before[:7]} → {after[:7]}" if after != before else None


def from_pipx():
    if time.time() - db.get("last_upgrade_check", 0) < 86400 or not shutil.which("pipx"):
        return
    db.put("last_upgrade_check", time.time())
    subprocess.run(["pipx", "upgrade", "prdash"], capture_output=True, timeout=300)


def on_start(argv):
    """Called before anything else runs. Restarts the process on the new code after a git update."""
    if (ROOT / ".git").exists():
        msg = from_git()
        if msg:
            print(f"prdash {msg}, restarting", flush=True)
            os.execv(sys.executable, [sys.executable, "-m", "prdash", *argv])
    elif "pipx" in sys.prefix:
        threading.Thread(target=from_pipx, daemon=True).start()
