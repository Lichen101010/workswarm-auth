import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from workswarm_auth.app import create_app
from workswarm_auth.config import ProviderSettings, Settings
from workswarm_auth.manager import Manager
from workswarm_auth.providers import Adapter, AuthError, DEFINITIONS, Registry
from workswarm_auth.vault import Vault


class VendorMock:
    def __init__(self):
        self.requests = []
        self.refreshes = 0
        self.probe_denied = False
        self.refresh_denied = False
        self.insufficient_scope = False
        self.identity_denied = False
        self.api_offline = False
        self.subject = "google-user-1"

    def __call__(self, request):
        self.requests.append(request)
        if self.api_offline and request.url.host == "gmail.googleapis.com":
            raise httpx.ConnectError("offline", request=request)
        if request.url.path.endswith(("/token", "/access_token", "/oauth.v2.access")) and request.method == "POST":
            form = parse_qs(request.content.decode())
            client_id = form.get("client_id", [""])[0]
            pid = client_id.removeprefix("client-")
            if form.get("grant_type") == ["refresh_token"]:
                self.refreshes += 1
                if self.refresh_denied:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "private secret"})
            scope = " ".join(DEFINITIONS[pid].scopes)
            if self.insufficient_scope: scope = "unrelated_scope"
            payload = {"access_token": "private-access-" + pid, "refresh_token": f"private-refresh-{pid}-{self.refreshes}",
                       "expires_in": 3600, "scope": scope}
            if pid == "slack": payload.update(ok=True, token_type="bot")
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("/userinfo"):
            if self.identity_denied:
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"sub": self.subject})
        if request.url.path == "/v1.0/me":
            return httpx.Response(200, json={"id": "graph-user-1"})
        if self.probe_denied:
            return httpx.Response(403, json={"error": {"message": "private project details", "details": [{"reason": "SERVICE_DISABLED"}]}})
        if request.url.path.endswith("/revoke") or request.url.path.endswith("auth.revoke"):
            return httpx.Response(200, json={"ok": True})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/user": return httpx.Response(200, json={"login": "test", "id": 1})
        if request.url.path.endswith("/repos"): return httpx.Response(200, json=[{"id": 1, "full_name": "test/repo"}])
        if request.url.path.endswith("auth.test"): return httpx.Response(200, json={"ok": True, "user_id": "U1", "team_id": "T1"})
        return httpx.Response(200, json={"messages": [], "files": [], "items": [], "value": []})


TEST_ISSUER = "https://test-issuer.example"
TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(TEST_KEY.public_key()))
TEST_JWK.update(kid="test-issuer", alg="RS256", use="sig")


def server_config():
    policy = {"providers": list(DEFINITIONS), "operations": sorted({op for item in DEFINITIONS.values() for op in item.operations})}
    return {"issuer": TEST_ISSUER, "jwks_uri": TEST_ISSUER + "/keys", "public_origin": "https://broker.example",
            "principals": {"local": policy, "alice": policy},
            "owner_login": {"client_id": "browser-client", "authorization_endpoint": TEST_ISSUER + "/authorize", "token_endpoint": TEST_ISSUER + "/token"}}


def owner_id(subject="local"):
    return "oidc_" + hashlib.sha256(json.dumps([TEST_ISSUER, subject], separators=(",", ":")).encode()).hexdigest()[:48]


def headers(owner="local"):
    token = jwt.encode({"iss": TEST_ISSUER, "sub": owner, "aud": "https://broker.example/mcp", "exp": int(time.time()) + 300,
                        "scope": "auth:read auth:connect auth:use auth:disconnect"}, TEST_KEY,
                       algorithm="RS256", headers={"kid": "test-issuer"})
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def rig(tmp_path):
    configs = {pid: ProviderSettings("client-" + pid, "private-client-secret") for pid in DEFINITIONS}
    configs["slack"] = ProviderSettings("client-slack", "private-client-secret", pkce=False)
    settings = Settings(tmp_path, configs, remote=server_config())
    vendor = VendorMock()
    owner_login = {}
    def handler(request):
        if request.url.host == "test-issuer.example":
            if request.url.path == "/keys":
                return httpx.Response(200, json={"keys": [TEST_JWK]})
            token = jwt.encode({"iss": TEST_ISSUER, "sub": owner_login["subject"], "aud": "browser-client",
                                "exp": int(time.time()) + 300, "nonce": owner_login["nonce"]}, TEST_KEY,
                               algorithm="RS256", headers={"kid": "test-issuer"})
            return httpx.Response(200, json={"id_token": token})
        return vendor(request)
    app = create_app(settings, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False))
    app.state.test_owner_login = owner_login
    with TestClient(app, base_url=settings.origin) as client:
        yield client, app, vendor, settings


def begin(rig, pid="gmail", cid="account-one", owner="local"):
    client, app, *_ = rig
    response = client.post("/api/auth/connections", headers=headers(owner), json={"provider": pid, "connection_id": cid})
    assert response.status_code == 200, response.text
    link = response.json()["connect_url"]
    redirect = client.get(link, follow_redirects=False)
    assert redirect.status_code == 302
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    app.state.test_owner_login.update(subject=owner, nonce=query["nonce"][0])
    redirect = client.get("/oauth/owner/callback", params={"state": query["state"][0], "code": "owner-code"}, follow_redirects=False)
    assert redirect.status_code == 302, redirect.text
    params = parse_qs(urlsplit(redirect.headers["location"]).query)
    return params, link


def authorize(rig, pid="gmail", cid="account-one", owner="local"):
    params, _ = begin(rig, pid, cid, owner)
    response = rig[0].get(f"/oauth/providers/{pid}/callback", params={"state": params["state"][0], "code": "temporary-code"})
    assert response.status_code == 200, response.text
    return params


@pytest.mark.parametrize("pid", DEFINITIONS)
def test_each_provider_oauth_and_actual_probe_are_separate(rig, pid):
    params = authorize(rig, pid)
    client, app, vendor, _ = rig
    status = client.get("/api/auth/connections/account-one", headers=headers()).json()
    assert status["authorization"] == "authorized"
    assert status["validation"] == "not_checked"
    result = client.post("/api/auth/connections/account-one/validate", headers=headers())
    assert result.status_code == 200, result.text
    assert result.json()["validation"] == "passed"
    assert result.json()["probe"] == DEFINITIONS[pid].probe
    assert "data" not in result.json()
    assert "private-" not in result.text
    raw = app.state.vault.path.read_bytes()
    assert b"private-access" not in raw and b"private-refresh" not in raw
    token_request = next(req for req in vendor.requests if req.method == "POST")
    form = parse_qs(token_request.content.decode())
    if pid == "slack":
        assert "code_challenge" not in params and "code_verifier" not in form
    else:
        expected = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        assert params["code_challenge"] == [expected]


def test_owners_and_accounts_are_isolated(rig):
    authorize(rig, cid="shared-name")
    client = rig[0]
    assert client.get("/api/auth/connections/shared-name", headers=headers("alice")).status_code == 404
    assert client.post("/api/auth/connections/shared-name/validate", headers=headers("alice")).status_code == 404
    assert client.post("/api/auth/connections/shared-name/disconnect", headers=headers("alice")).status_code == 404
    authorize(rig, "github", "shared-name", "alice")
    authorize(rig, "gmail", "second-account")
    local = client.get("/api/auth/connections", headers=headers()).json()["connections"]
    alice = client.get("/api/auth/connections", headers=headers("alice")).json()["connections"]
    assert len(local) == 2 and len(alice) == 1
    client.post("/api/auth/connections/shared-name/disconnect", headers=headers())
    assert client.get("/api/auth/connections/second-account", headers=headers()).json()["authorization"] == "authorized"
    assert client.get("/api/auth/connections/shared-name", headers=headers("alice")).json()["authorization"] == "authorized"


def test_denied_probe_does_not_claim_authorization_failed(rig):
    authorize(rig)
    rig[2].probe_denied = True
    result = rig[0].post("/api/auth/connections/account-one/validate", headers=headers())
    assert result.status_code == 403
    assert result.json()["error"] == "provider_api_not_enabled"
    status = rig[0].get("/api/auth/connections/account-one", headers=headers()).json()
    assert status["authorization"] == "authorized" and status["validation"] == "failed"
    assert "private project" not in result.text


def test_state_is_single_use_provider_bound_and_expiring(rig):
    client, app, _, _ = rig
    params, link = begin(rig)
    assert client.get(link).status_code == 400
    assert client.get("/oauth/providers/github/callback", params={"state": params["state"][0], "code": "code"}).status_code == 400
    assert client.get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "code": "code"}).status_code == 400
    params = authorize(rig)
    assert client.get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "code": "code"}).status_code == 400
    state = app.state.vault.issue("oauth_state", {"provider": "gmail"}, ttl=-1)
    assert client.get("/oauth/providers/gmail/callback", params={"state": state, "code": "code"}).status_code == 400


def test_cancel_reconnect_and_disconnect_invalidate_old_flows(rig):
    params, _ = begin(rig)
    client = rig[0]
    assert client.get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "error": "access_denied"}).status_code == 400
    assert client.get("/api/auth/connections/account-one", headers=headers()).json()["authorization"] == "failed"
    params, _ = begin(rig)
    client.post("/api/auth/connections/account-one/disconnect", headers=headers())
    assert client.get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "code": "late"}).status_code == 409
    old, _ = begin(rig)
    authorize(rig)
    assert client.get("/oauth/providers/gmail/callback", params={"state": old["state"][0], "code": "late"}).status_code == 409


def test_insufficient_scope_not_saved(rig):
    params, _ = begin(rig)
    rig[2].insufficient_scope = True
    response = rig[0].get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "code": "code"})
    assert response.status_code == 403
    assert rig[1].state.vault.credentials(owner_id("local"), "account-one") is None


@pytest.mark.parametrize("pid", ["gmail", "github", "microsoft", "slack"])
def test_refresh_rotates_and_revocation_requires_reauth(rig, pid):
    authorize(rig, pid)
    client, app, vendor, _ = rig
    vault = app.state.vault
    info = vault.info(owner_id("local"), "account-one")
    tokens = vault.credentials(owner_id("local"), "account-one")
    tokens["expires_at"] = 0
    vault.save(owner_id("local"), "account-one", info["revision"], tokens)
    response = client.post("/api/auth/connections/account-one/validate", headers=headers())
    assert response.status_code == 200, response.text
    assert vendor.refreshes == 1
    assert vault.credentials(owner_id("local"), "account-one")["refresh_token"].endswith("-1")
    tokens = vault.credentials(owner_id("local"), "account-one")
    tokens["expires_at"] = 0
    vault.save(owner_id("local"), "account-one", info["revision"], tokens)
    vendor.refresh_denied = True
    assert client.post("/api/auth/connections/account-one/validate", headers=headers()).status_code == 401
    assert vault.credentials(owner_id("local"), "account-one") is None


def test_concurrent_refresh_consumes_rotating_token_once(tmp_path):
    async def run():
        vendor = VendorMock()
        settings = Settings(tmp_path, {"slack": ProviderSettings("client-slack", "secret", pkce=False)}, remote=server_config())
        vault = Vault(tmp_path)
        revision, flow = vault.begin("local", "one", "slack", "")
        vault.complete("local", "one", revision, flow, {"access_token": "old", "refresh_token": "rotation-old", "scope": "team:read", "expires_at": 0})
        async with httpx.AsyncClient(transport=httpx.MockTransport(vendor), trust_env=False) as client:
            manager = Manager(settings, vault, Registry(settings, client))
            results = await asyncio.gather(*[manager.access_token("local", "one") for _ in range(5)])
            assert len({item[0] for item in results}) == 1
            assert vendor.refreshes == 1
    asyncio.run(run())


def test_only_allowlisted_read_operations_and_hosts(rig):
    authorize(rig)
    client, _, vendor, _ = rig
    count = len(vendor.requests)
    response = client.post("/api/auth/connections/account-one/operations", headers=headers(),
        json={"operation": "send_mail", "arguments": {}})
    assert response.status_code == 403
    response = client.post("/api/auth/connections/account-one/operations", headers=headers(),
        json={"operation": "gmail_search", "arguments": {"url": "https://attacker.example"}})
    assert response.status_code == 400
    assert len(vendor.requests) == count
    assert client.get("/api/auth/providers").status_code == 401
    assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 400


def test_slack_pkce_user_normalization_and_no_secret():
    async def run():
        seen = []
        def handler(request):
            seen.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"ok": True, "authed_user": {
                "access_token": "private-user", "refresh_token": "rotate", "scope": "team:read", "token_type": "user", "expires_in": 3600}})
        config = ProviderSettings("client-slack", "must-not-be-sent", "https://app.example/oauth/providers/slack/callback", token_kind="user", pkce=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            adapter = Adapter(DEFINITIONS["slack"], config, client)
            url = adapter.authorization_url("state", "verifier", config.redirect_uri)
            assert "user_scope=team%3Aread" in url and "code_challenge" in url
            result = await adapter.exchange("code", "verifier", config.redirect_uri)
            assert result["access_token"] == "private-user"
            assert "client_secret" not in seen[0]
    asyncio.run(run())


def test_microsoft_and_rotating_slack_report_manual_revocation(rig):
    for pid, cid in [("microsoft", "ms"), ("slack", "sl")]:
        authorize(rig, pid, cid)
        result = rig[0].post(f"/api/auth/connections/{cid}/disconnect", headers=headers()).json()
        assert result["credentials_deleted"] and not result["remote_revoked"]
        assert result["manual_revoke_url"]


def test_reconnect_keeps_working_credentials_until_new_grant_is_verified(rig):
    authorize(rig)
    client, app, _, _ = rig
    vault = app.state.vault
    original = vault.credentials(owner_id("local"), "account-one")
    # Starting, abandoning or denying a reconnection must not break the existing grant.
    params, _ = begin(rig)
    assert client.get("/api/auth/connections/account-one", headers=headers()).json()["pending"] is True
    assert client.post("/api/auth/connections/account-one/operations", headers=headers(),
                       json={"operation": "gmail_search", "arguments": {}}).status_code == 200
    denied = client.get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "error": "access_denied"})
    assert denied.status_code == 400
    status = client.get("/api/auth/connections/account-one", headers=headers()).json()
    assert status["authorization"] == "authorized" and status["pending"] is False and status["last_error"] == "user_denied"
    assert vault.credentials(owner_id("local"), "account-one") == original
    # Completing it swaps credentials atomically and fences anything bound to the old revision.
    revision = status["revision"]
    params, _ = begin(rig)
    assert client.get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "code": "new"}).status_code == 200
    assert vault.info(owner_id("local"), "account-one")["revision"] == revision + 1
    assert not vault.save(owner_id("local"), "account-one", revision, original)


def test_identity_lookup_failure_after_refresh_keeps_grant(rig):
    authorize(rig)
    client, app, vendor, _ = rig
    vault = app.state.vault
    info = vault.info(owner_id("local"), "account-one")
    tokens = vault.credentials(owner_id("local"), "account-one")
    tokens["expires_at"] = 0
    vault.save(owner_id("local"), "account-one", info["revision"], tokens)
    vendor.identity_denied = True
    response = client.post("/api/auth/connections/account-one/validate", headers=headers())
    assert response.status_code == 401 and response.json()["error"] == "provider_identity_missing_reconnect_required"
    status = client.get("/api/auth/connections/account-one", headers=headers()).json()
    assert status["authorization"] == "authorized" and status["validation"] == "failed"
    # The rotated refresh token was persisted, not discarded.
    assert vault.credentials(owner_id("local"), "account-one")["refresh_token"].endswith("-1")
    assert status["identity_pending"] is True
    # The unverified access token is not handed to API calls: identity is retried first.
    count = len(vendor.requests)
    search = {"operation": "gmail_search", "arguments": {}}
    response = client.post("/api/auth/connections/account-one/operations", headers=headers(), json=search)
    assert response.status_code == 401 and response.json()["error"] == "provider_identity_missing_reconnect_required"
    assert [req.url.path for req in vendor.requests[count:]] == ["/v1/userinfo"]
    vendor.identity_denied = False
    assert client.post("/api/auth/connections/account-one/operations", headers=headers(), json=search).status_code == 200
    assert client.get("/api/auth/connections/account-one", headers=headers()).json()["identity_pending"] is False


def test_transient_vendor_error_keeps_last_validation_result(rig):
    authorize(rig)
    client, app, vendor, _ = rig
    assert client.post("/api/auth/connections/account-one/validate", headers=headers()).status_code == 200
    vendor.api_offline = True
    response = client.post("/api/auth/connections/account-one/validate", headers=headers())
    assert response.status_code == 502 and response.json()["error"] == "provider_network_error"
    assert client.get("/api/auth/connections/account-one", headers=headers()).json()["validation"] == "passed"


