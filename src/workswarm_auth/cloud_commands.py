"""Desktop installation and CLI operations; no provider credentials are read."""
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .cloud import CloudClient, CloudProfile
from .cloud_mcp import cid_path, wait_for_connection
from .errors import AuthError
from .identifiers import SAFE_ID

# Present only in a publisher-built client wheel; the source tree has no profile.
BUNDLED_PROFILE = Path(__file__).with_name("publisher_profile.json")


def configuration(root):
    return {"mcpServers": {"workswarm-auth-cloud": {"type": "stdio", "command": sys.executable,
        "args": ["-X", "utf8", "-m", "workswarm_auth.cli", "cloud-mcp", "--home", str(root)]}}}


def write_profile(root, profile):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = root / "cloud.json"
    if destination.exists() and CloudProfile.load(destination) != profile:
        raise ValueError("cloud_profile_already_exists_use_another_home")
    destination.write_text(json.dumps(asdict(profile), indent=2) + "\n", encoding="utf-8")


def run(args, root):
    if args.command == "cloud-init":
        if args.profile is None:
            raise ValueError("publisher_cloud_profile_required")
        write_profile(root, CloudProfile.load(args.profile))
        print('{"success":true,"provider_credentials_required":false}')
        return 0
    if not (root / "cloud.json").exists() and BUNDLED_PROFILE.is_file():
        write_profile(root, CloudProfile.load(BUNDLED_PROFILE))
    if args.command == "cloud-config":
        CloudProfile.load(root / "cloud.json")
        print(json.dumps(configuration(root), indent=2))
        return 0
    if args.command == "cloud-mcp":
        from .cloud_mcp import build_cloud_mcp
        build_cloud_mcp(root).run(transport="stdio")
        return 0
    client = CloudClient(root)
    if args.command == "cloud-login":
        print(json.dumps(client.login(announce=lambda url: print(url, flush=True))))
        return 0
    if args.command == "cloud-logout":
        print(json.dumps(client.logout()))
        return 0
    if args.command == "cloud-status":
        print(json.dumps(client.request("GET", "/api/auth/session")))
        return 0
    if args.command == "cloud-connect":
        if not SAFE_ID.fullmatch(args.provider):
            raise ValueError("invalid_provider")
        try:
            client.access_token()
        except AuthError as error:
            if error.code != "workswarm_login_required":
                raise
            client.login(announce=lambda url: print(url, flush=True))
        started = client.request("POST", "/api/auth/connections", json={
            "provider": args.provider, "connection_id": args.connection or None})
        print(started["connect_url"], flush=True)
        client.open_connect_url(started["connect_url"])
        try:
            result = wait_for_connection(client, started, args.wait_seconds)
        except KeyboardInterrupt:
            client.request("POST", cid_path(started["connection_id"]) + "/cancel",
                           json={"attempt_id": started["attempt_id"]})
            return 1
        print(json.dumps(result))
        return 0
    raise ValueError("unknown_cloud_command")
