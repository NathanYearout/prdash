"""prdash: a fast local dashboard for your GitHub PRs. Needs an authenticated `gh`."""
import argparse, os, shutil, sys, tempfile, threading, webbrowser
from urllib.request import urlopen

if "--demo" in sys.argv:  # must happen before db is imported: the demo gets a throwaway database
    os.environ["PRDASH_HOME"] = tempfile.mkdtemp(prefix="prdash-demo-")

from . import db, demo, github, server, sync


def already_running(url):
    try:
        with urlopen(url, timeout=1) as r:
            return b"<title>prdash</title>" in r.read(4096)
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(prog="prdash", description=__doc__)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PRDASH_PORT", 8765)))
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--demo", action="store_true", help="try it with a fictional org; nothing touches GitHub")
    ap.add_argument("command", nargs="*", help="`service install|uninstall|status` to run prdash in the background from login")
    args = ap.parse_args()
    if args.command[:1] == ["service"]:
        from . import service
        return service.main((args.command[1:] or [""])[0], args.port)
    if args.command:
        ap.error(f"unknown command {' '.join(args.command)}")
    url = f"http://127.0.0.1:{args.port}/"
    if already_running(url):  # running `prdash` again just brings the dashboard back
        print(f"prdash is already running at {url}")
        return webbrowser.open(url) if not args.no_open else None
    if args.demo:
        demo.seed()
        server.DEMO = True
        print(f"prdash demo on {url}", flush=True)
        if not args.no_open:
            threading.Timer(0.3, webbrowser.open, (url,)).start()
        return server.serve(args.port)
    if not shutil.which("gh"):
        sys.exit("prdash needs the GitHub CLI: https://cli.github.com, then run `gh auth login`.")
    try:
        me = github.viewer()
    except RuntimeError as e:
        sys.exit(f"gh isn't logged in ({e}). Run `gh auth login`.")
    print(f"prdash on {url} as {me}, searching {', '.join(github.owners())}. Data: {db.HOME}", flush=True)
    sync.start()
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        server.serve(args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
