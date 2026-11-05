"""Market launcher: install the publisher's client on first start, then run the stdio bridge.

build_cloud_client.py fills VERSION and ORIGIN and embeds this file (base64) in mcp.json, run as
``py -3 -c ...``. Everything except the bridge itself writes to stderr: stdout carries MCP JSON-RPC,
and the bridge inherits stdin/stdout directly so no shell re-encodes the protocol stream.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request

VERSION = "__VERSION__"
ORIGIN = "__ORIGIN__"


def log(message):
    print("[workswarm-auth] " + message, file=sys.stderr, flush=True)


def main():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    home = pathlib.Path(base) / "WorkSwarmAuth" / VERSION
    venv = home / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        if sys.version_info < (3, 11):
            log("Python 3.11+ is required; found " + sys.version.split()[0] + ". Install Python 3.12 and reconnect.")
            return 1
        log("first start: installing client " + VERSION + " from " + ORIGIN)
        wheel = home / ("workswarm_auth-" + VERSION + "-py3-none-any.whl")
        try:
            home.mkdir(parents=True, exist_ok=True)
            # urllib uses the OS certificate store on Windows, unlike the bridge's certifi bundle.
            with urllib.request.urlopen(ORIGIN + "/client/" + wheel.name, timeout=60) as response:
                wheel.write_bytes(response.read())
            subprocess.check_call([sys.executable, "-m", "venv", str(venv)], stdout=sys.stderr)
            subprocess.check_call([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", str(wheel)],
                                  stdout=sys.stderr)
        except (OSError, subprocess.CalledProcessError) as error:
            # Leave nothing half-installed so the next connect retries from scratch.
            shutil.rmtree(venv, ignore_errors=True)
            log("installation failed: " + type(error).__name__ + ": " + str(error))
            return 1
        log("installed")
    return subprocess.call([str(python), "-X", "utf8", "-m", "workswarm_auth.cli", "cloud-mcp"])


if __name__ == "__main__":
    sys.exit(main())
