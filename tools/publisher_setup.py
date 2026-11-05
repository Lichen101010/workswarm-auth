"""Render publisher-only deployment and public desktop profile. Does not deploy.

Run from the source tree after installing the package. Generated secrets remain
in the output directory; never include it in a client release or source archive.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
import httpx

from workswarm_auth.cloud import CloudProfile, secure_url
from workswarm_auth.config import Settings
from workswarm_auth.providers import DEFINITIONS

# Provider settings that differ from the defaults: Microsoft accepts work and personal accounts,
# and Slack only supports PKCE once enabled per app, so it uses the client secret.
PROVIDER_DEFAULTS = {"microsoft": {"tenant": "common"}, "slack": {"pkce": False, "token_kind": "bot"}}


def render(output, broker_url, issuer, identity_domain=None, providers=("gmail", "github")):
    profile = CloudProfile(broker_url, issuer)
    if output.exists():
        raise ValueError("publisher_output_exists_choose_another_directory")
    group = "workswarm-users"
    unknown = [pid for pid in providers if pid not in DEFINITIONS]
    if unknown or not providers or len(set(providers)) != len(providers):
        raise ValueError("publisher_providers_must_be_distinct_known_ids")
    policy = {"providers": list(providers),
              "operations": [op for pid in providers for op in DEFINITIONS[pid].operations]}
    endpoints = {"jwks_uri": issuer + "/protocol/openid-connect/certs",
                 "authorization_endpoint": issuer + "/protocol/openid-connect/auth",
                 "token_endpoint": issuer + "/protocol/openid-connect/token"}
    if not identity_domain:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            response = client.get(issuer + "/.well-known/openid-configuration")
            response.raise_for_status()
            discovered = response.json()
        if not isinstance(discovered, dict) or discovered.get("issuer") != issuer:
            raise ValueError("publisher_issuer_metadata_mismatch")
        for name in endpoints:
            endpoint = secure_url(discovered.get(name))
            if urlsplit(endpoint).netloc != urlsplit(issuer).netloc:
                raise ValueError("publisher_identity_endpoints_must_share_issuer_authority")
            endpoints[name] = endpoint
    remote = {"issuer": issuer, "jwks_uri": endpoints["jwks_uri"],
              "public_origin": broker_url, "principals": {}, "enrollment": {"group": group, **policy},
              "owner_login": {"client_id": "workswarm-auth-browser", "client_secret": "",
                 "authorization_endpoint": endpoints["authorization_endpoint"],
                 "token_endpoint": endpoints["token_endpoint"]}}
    Settings(output, remote=remote)
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    config = output / "config"
    config.mkdir(mode=0o700)
    (output / "data").mkdir(mode=0o700)
    (output / "secrets").mkdir(mode=0o700)
    key_path = output / "secrets/vault.key"
    key_path.write_bytes(Fernet.generate_key())
    key_path.chmod(0o600)
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
    write(output / "cloud.json", profile.binding)
    write(config / "remote.json", remote)
    write(config / "providers.json", {pid: {"client_id": "", "client_secret": "",
          "redirect_uri": broker_url + f"/oauth/providers/{pid}/callback", **PROVIDER_DEFAULTS.get(pid, {})}
          for pid in policy["providers"]})
    (config / "config.env").write_text("AUTH_PORT=8766\n", encoding="utf-8")
    template_root = Path(__file__).resolve().parents[1] / "deployment/cloud"
    shutil.copy2(template_root / "compose.yaml", output / "compose.yaml")
    caddy = f"{urlsplit(broker_url).netloc} {{\n    reverse_proxy broker:8766\n}}\n"
    env = "CADDY_IMAGE=caddy:2\n"
    if identity_domain:
        secure_url("https://" + identity_domain, origin=True)
        caddy += f"\n{identity_domain} {{\n    reverse_proxy identity:8080\n}}\n"
        shutil.copy2(template_root / "compose.identity.yaml", output / "compose.identity.yaml")
        env += ("IDENTITY_DOMAIN=" + identity_domain + "\nPOSTGRES_IMAGE=postgres:17\n"
                "POSTGRES_PASSWORD=" + secrets.token_urlsafe(40) + "\n"
                "KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD=" + secrets.token_urlsafe(40) + "\n"
                "# Set an approved production Keycloak image, preferably pinned by digest.\nKEYCLOAK_IMAGE=\n")
        write(output / "realm.json", realm(profile, group))
        # Keycloak runs as its own uid and must read the import; it holds only public clients.
        (output / "realm.json").chmod(0o644)
    (output / "Caddyfile").write_text(caddy, encoding="utf-8")
    (output / ".env").write_text(env, encoding="utf-8")
    for path in (config / "config.env", output / ".env"):
        path.chmod(0o600)
    return {"created": True, "directory": str(output), "public_profile": str(output / "cloud.json"),
            "deployed": False, "provider_configuration_required": True}


def realm(profile, group):
    scopes = ["auth:read", "auth:connect", "auth:use", "auth:disconnect"]
    groups_mapper = {"name": "workswarm-group-membership", "protocol": "openid-connect",
                     "protocolMapper": "oidc-group-membership-mapper", "consentRequired": False,
                     "config": {"claim.name": "groups", "full.path": "false", "id.token.claim": "true",
                                "access.token.claim": "true", "userinfo.token.claim": "true"}}
    audience = {"name": "workswarm-broker-audience", "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper", "consentRequired": False,
                "config": {"included.custom.audience": profile.resource,
                           "id.token.claim": "false", "access.token.claim": "true"}}
    # Declaring clientScopes suppresses Keycloak's built-in scopes; since Keycloak 25 the
    # access-token `sub` comes only from `basic`, and the broker rejects tokens without it.
    basic = {"name": "basic", "protocol": "openid-connect",
             "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
             "protocolMappers": [{"name": "sub", "protocol": "openid-connect", "protocolMapper": "oidc-sub-mapper",
                                  "consentRequired": False,
                                  "config": {"access.token.claim": "true", "introspection.token.claim": "true"}}]}
    def client(cid, redirect, desktop=False):
        return {"clientId": cid, "protocol": "openid-connect", "publicClient": True,
                "enabled": True, "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False, "redirectUris": [redirect], "webOrigins": [],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "defaultClientScopes": ["basic", *(scopes if desktop else [])],
                "protocolMappers": [groups_mapper, *([audience] if desktop else [])]}
    return {"realm": "workswarm", "enabled": True, "sslRequired": "external", "registrationAllowed": False,
            "groups": [{"name": group}], "clients": [
                client(profile.client_id, f"http://127.0.0.1:{profile.callback_port}/oauth/callback", True),
                client("workswarm-auth-browser", profile.server_url + "/oauth/owner/callback")],
            "clientScopes": [basic, *({"name": name, "protocol": "openid-connect",
                              "attributes": {"include.in.token.scope": "true"}} for name in scopes)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-origin", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--identity-domain", help="Render a managed Keycloak realm on this hostname")
    target.add_argument("--issuer", help="Existing OIDC issuer; fetch and validate its discovery metadata")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--providers", default="gmail,github",
                        help="Comma-separated: " + ",".join(DEFINITIONS))
    args = parser.parse_args()
    issuer = args.issuer or "https://" + args.identity_domain + "/realms/workswarm"
    providers = tuple(item.strip() for item in args.providers.split(",") if item.strip())
    print(json.dumps(render(args.output.resolve(), args.public_origin, issuer, args.identity_domain, providers)))


if __name__ == "__main__":
    main()
