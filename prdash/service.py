"""`prdash service install|uninstall|status`: keep prdash running in the background from login.

Linux uses a systemd user unit, macOS a LaunchAgent. Your PRDASH_* environment is copied in,
so a launcher script's settings carry over.
"""
import os, platform, subprocess, sys
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "dev.prdash"
ROOT = Path(__file__).resolve().parent.parent  # so `-m prdash` also works from a source checkout


def _env():
    """PRDASH_* settings for the service, minus secrets: tokens belong in prdash's owner-only database
    (Settings page), not in a unit file."""
    from .config import FIELDS
    secret = {"PRDASH_" + k.upper() for k, f in FIELDS.items() if f[1] == "secret"}
    keep = {k: v for k, v in os.environ.items() if k.startswith("PRDASH_") and k not in secret}
    return keep | {"PATH": os.environ.get("PATH", "")}


def _sd(value):
    """Quote one value for a systemd unit: escape backslash and quote, double % (systemd specifiers)."""
    if "\n" in value:
        raise SystemExit(f"can't put a newline in the service file: {value!r}")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)  # also tightens a file left by an older install


def _argv(port):
    return [sys.executable, "-m", "prdash", "--no-open", "--port", str(port)]


def _run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r.stdout


def _systemd_unit():
    return Path.home() / ".config/systemd/user/prdash.service"


def _launchd_plist():
    return Path.home() / f"Library/LaunchAgents/{LABEL}.plist"


def install(port):
    if platform.system() == "Darwin":
        args = "".join(f"<string>{escape(a)}</string>" for a in _argv(port))
        env = "".join(f"<key>{escape(k)}</key><string>{escape(v)}</string>" for k, v in _env().items())
        plist = _launchd_plist()
        _write_private(plist, f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>EnvironmentVariables</key><dict>{env}</dict>
<key>WorkingDirectory</key><string>{escape(str(ROOT))}</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>{escape(str(Path.home() / "Library/Logs/prdash.log"))}</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        _run("launchctl", "load", "-w", str(plist))
    elif platform.system() == "Linux":
        unit = _systemd_unit()
        env = "\n".join(f"Environment={_sd(f'{k}={v}')}" for k, v in _env().items())
        _write_private(unit, f"""[Unit]
Description=prdash, a local dashboard for GitHub pull requests
After=network-online.target

[Service]
ExecStart={" ".join(_sd(a) for a in _argv(port))}
WorkingDirectory={str(ROOT).replace("%", "%%")}
{env}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
        _run("systemctl", "--user", "daemon-reload")
        _run("systemctl", "--user", "enable", "--now", "prdash.service")
    else:
        raise SystemExit("prdash service supports Linux (systemd) and macOS. On Windows, add `prdash --no-open` to Startup.")
    print(f"prdash now starts at login and is running at http://127.0.0.1:{port}/ . Run `prdash` any time to open it.")


def uninstall():
    if platform.system() == "Darwin":
        subprocess.run(["launchctl", "unload", "-w", str(_launchd_plist())], capture_output=True)
        _launchd_plist().unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", "prdash.service"], capture_output=True)
        _systemd_unit().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("prdash service removed.")


def status():
    if platform.system() == "Darwin":
        print("installed" if _launchd_plist().exists() else "not installed")
        subprocess.run(["launchctl", "list", LABEL])
    else:
        subprocess.run(["systemctl", "--user", "status", "--no-pager", "prdash.service"])


def main(action, port):
    {"install": lambda: install(port), "uninstall": uninstall, "status": status}.get(
        action, lambda: sys.exit("usage: prdash service install|uninstall|status"))()
