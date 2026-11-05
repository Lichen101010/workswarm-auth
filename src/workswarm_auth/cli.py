"""Cloud desktop client and publisher server commands."""
import argparse
import json
import os
from pathlib import Path

import httpx

from .errors import AuthError

CLOUD_COMMANDS = ("cloud-init", "cloud-config", "cloud-login", "cloud-logout", "cloud-status", "cloud-connect", "cloud-mcp")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="WorkSwarm cloud authorization")
    parser.add_argument("command", choices=["serve", *CLOUD_COMMANDS])
    parser.add_argument("--home", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--provider", default="gmail")
    parser.add_argument("--connection", default="")
    parser.add_argument("--wait-seconds", type=int, default=180)
    parser.add_argument("--listen", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    args = parser.parse_args()
    default_root = Path.home() / (".workswarm-auth-cloud" if args.command in CLOUD_COMMANDS else ".workswarm-auth-server")
    root = (args.home or Path(os.environ.get("WORKSWARM_AUTH_HOME", default_root))).expanduser().resolve()
    try:
        if args.command in CLOUD_COMMANDS:
            from .cloud_commands import run
            raise SystemExit(run(args, root))
        try:
            import uvicorn
            from .config import load_settings
            from .app import create_app
        except ImportError:
            raise ValueError("server_components_required_install_publisher_package") from None
        settings = load_settings(root)
        uvicorn.run(create_app(settings), host=args.listen, port=settings.port, access_log=False, workers=1)
    except AuthError as error:
        print(json.dumps({"success": False, "error": error.code, "http_status": error.status}))
        raise SystemExit(1)
    except (ValueError, OSError, TypeError, httpx.HTTPError) as error:
        # Never echo endpoint URLs, provider responses, tokens or private configuration.
        message = str(error) if type(error) is ValueError and str(error).replace("_", "").isalnum() else type(error).__name__
        parser.exit(1, "Configuration/request failed: " + message + "\n")


if __name__ == "__main__":
    main()
