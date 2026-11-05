"""Let users sign in to the managed Keycloak realm with Google or GitHub, and enroll them automatically.

Runs on the publisher server against the Keycloak admin API (bootstrap admin from the runtime .env).
Client secrets come from hidden input or, with --reuse, from the broker's providers.json on the same
server; they are never printed. Register this redirect URI in the Google/GitHub app first:
  <issuer>/broker/<google|github>/endpoint
With --auto-enroll every new user, including one created by a Google/GitHub sign-in, joins the
enrollment group. That admits anyone who can sign in with the linked provider, so pair it with a
provider app limited to the intended users (e.g. Google OAuth "Testing" test users or a Workspace app).
"""
import argparse
import getpass
import json
from pathlib import Path

import httpx

REALM = "workswarm"
GROUP = "workswarm-users"
SCOPES = {"google": "openid email profile", "github": "read:user user:email"}


def env_values(path):
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line and not line.startswith("#"))
    return {key.strip(): value.strip() for key, value in pairs}


def admin_client(base, user, password, client=None):
    client = client or httpx.Client(timeout=20, trust_env=False)
    response = client.post(f"{base}/realms/master/protocol/openid-connect/token",
                           data={"grant_type": "password", "client_id": "admin-cli", "username": user, "password": password})
    response.raise_for_status()
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    return client


def configure(client, base, providers, credentials, auto_enroll):
    admin = f"{base}/admin/realms/{REALM}"
    if auto_enroll:
        response = client.get(f"{admin}/groups", params={"search": GROUP, "exact": "true"})
        response.raise_for_status()
        groups = response.json()
        group_id = next(item["id"] for item in groups if item["name"] == GROUP)
        client.put(f"{admin}/default-groups/{group_id}").raise_for_status()
    for alias in providers:
        client_id, secret = credentials[alias]
        body = {"alias": alias, "providerId": alias, "enabled": True, "trustEmail": True,
                "firstBrokerLoginFlowAlias": "first broker login",
                "config": {"clientId": client_id, "clientSecret": secret, "defaultScope": SCOPES[alias], "syncMode": "IMPORT"}}
        existing = client.get(f"{admin}/identity-provider/instances/{alias}")
        if existing.status_code == 404:
            client.post(f"{admin}/identity-provider/instances", json=body).raise_for_status()
        else:
            existing.raise_for_status()
            client.put(f"{admin}/identity-provider/instances/{alias}", json=body).raise_for_status()
    return {"identity_providers": list(providers), "auto_enroll_group": GROUP if auto_enroll else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", type=Path, required=True, help="publisher_setup output directory")
    parser.add_argument("--keycloak-url", help="Keycloak base URL reachable from here (default: https://IDENTITY_DOMAIN)")
    parser.add_argument("--providers", default="google", help="Comma-separated: google,github")
    parser.add_argument("--reuse", action="append", default=[], metavar="LOGIN=PROVIDER",
                        help="Copy credentials from providers.json, e.g. google=gmail")
    parser.add_argument("--no-auto-enroll", dest="auto_enroll", action="store_false")
    args = parser.parse_args()
    env = env_values(args.runtime / ".env")
    base = (args.keycloak_url or "https://" + env["IDENTITY_DOMAIN"]).rstrip("/")
    providers = [item.strip() for item in args.providers.split(",") if item.strip()]
    if not providers or set(providers) - set(SCOPES):
        raise SystemExit("--providers must name google and/or github")
    reuse = dict(item.split("=", 1) for item in args.reuse)
    stored = json.loads((args.runtime / "config/providers.json").read_text(encoding="utf-8")) if reuse else {}
    credentials = {}
    for alias in providers:
        issuer = f"https://{env['IDENTITY_DOMAIN']}/realms/{REALM}"
        print(f"{alias}: redirect URI to register is {issuer}/broker/{alias}/endpoint")
        if alias in reuse:
            source = stored[reuse[alias]]
            credentials[alias] = (source["client_id"], source["client_secret"])
        else:
            credentials[alias] = (input(f"{alias} Client ID: ").strip(), getpass.getpass(f"{alias} Client Secret (hidden): ").strip())
        if not all(credentials[alias]):
            raise SystemExit(f"{alias}: both client ID and secret are required; nothing changed.")
    client = admin_client(base, env["KC_BOOTSTRAP_ADMIN_USERNAME"], env["KC_BOOTSTRAP_ADMIN_PASSWORD"])
    print(json.dumps(configure(client, base, providers, credentials, args.auto_enroll)))


if __name__ == "__main__":
    main()
