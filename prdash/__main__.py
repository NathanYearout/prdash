"""prdash: a fast local dashboard for your GitHub PRs. Needs an authenticated `gh`."""
import argparse, os, shutil, sys, tempfile, threading, webbrowser
from urllib.error import HTTPError
from urllib.request import urlopen

if "--demo" in sys.argv and "PRDASH_HOME" not in os.environ:  # before db is imported: the demo gets a throwaway database
    os.environ["PRDASH_HOME"] = tempfile.mkdtemp(prefix="prdash-demo-")

from . import config, db, demo, github, server, sync, update


def already_running(url):
    try:
        with urlopen(url, timeout=1) as r:
            body = r.read(4096)
    except HTTPError as e:  # a running prdash answers 401 until the browser logs in
        body = e.read(4096)
    except OSError:
        return False
    return b"<title>prdash</title>" in body


def main():
    ap = argparse.ArgumentParser(prog="prdash", description=__doc__)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PRDASH_PORT", 8765)))
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--demo", action="store_true", help="try it with a fictional org; nothing touches GitHub")
    ap.add_argument("--no-update", action="store_true", help="don't pull the latest prdash on start")
    ap.add_argument("command", nargs="*", help="`url` prints a one-time login link; `logout` signs out every browser; `service install|uninstall|status`")
    args = ap.parse_args()
    if args.command[:1] == ["service"]:
        from . import service
        return service.main((args.command[1:] or [""])[0], args.port)
    if args.command == ["url"]:  # a one-time login link, e.g. for another browser
        return print(server.login_url(args.port))
    if args.command == ["logout"]:
        server.logout_all()
        return print("Signed out every browser. Run `prdash` to sign in again.")
    if args.command:
        ap.error(f"unknown command {' '.join(args.command)}")
    if not (args.no_update or args.demo) and config.get("auto_update"):
        update.on_start([a for a in sys.argv[1:]])
    url = f"http://127.0.0.1:{args.port}/"
    if already_running(url):  # running `prdash` again just brings the dashboard back
        print(f"prdash is already running at {url}")
        return webbrowser.open(server.login_url(args.port)) if not args.no_open else None
    if args.demo:
        demo.seed()
        server.DEMO = True
        print(f"prdash demo on {url}", flush=True)
        if not args.no_open:
            threading.Timer(0.3, webbrowser.open, (server.login_url(args.port),)).start()
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
        threading.Timer(0.3, webbrowser.open, (server.login_url(args.port),)).start()
    try:
        server.serve(args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
