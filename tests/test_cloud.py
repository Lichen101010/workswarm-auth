"""Real loopback browser receiver + signed mock issuer + actual broker HTTP API.

No real provider credentials or internet requests. The issuer enforces PKCE and
the broker uses its ordinary JWT/cookie/account/API paths, not a bypass handler.
"""
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sys
import time
from urllib.parse import parse_qs, urlsplit
import zipfile

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

import workswarm_auth
from workswarm_auth.app import create_app
from workswarm_auth.cloud import CloudClient, CloudProfile, LoopbackLogin
from workswarm_auth.cloud_mcp import build_cloud_mcp, wait_for_connection
from workswarm_auth.config import ProviderSettings, Settings, load_settings
from workswarm_auth.providers import AuthError
from test_universal_auth import VendorMock, authorize, begin, headers, rig, owner_id


@pytest.fixture
def cloud_rig(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="issuer-key", alg="RS256", use="sig")
    issuer, broker = "https://identity.example/realms/workswarm", "https://broker.example"
    policy = {"providers": ["gmail", "github"], "operations": ["gmail_search", "gmail_read", "github_profile", "github_repos"]}
    remote = {"issuer": issuer, "jwks_uri": issuer + "/keys", "public_origin": broker,
              "principals": {}, "enrollment": {"group": "workswarm-users", **policy},
              "owner_login": {"client_id": "browser-client", "authorization_endpoint": issuer + "/authorize",
                              "token_endpoint": issuer + "/token"}}
    settings = Settings(tmp_path / "server", {
        pid: ProviderSettings("client-" + pid, "server-only-client-secret") for pid in ("gmail", "github")}, remote=remote)
    vendor = VendorMock()
    state = {"user": "alice", "nonce": None, "challenge": None, "identity_offline": False,
             "group": True, "rotation": 0, "refresh_denied": False}
    def sign(subject, audience, **claims):
        return jwt.encode({"iss": issuer, "sub": subject, "aud": audience, "exp": int(time.time()) + 300,
                           "groups": ["workswarm-users"] if state["group"] else [], **claims}, key,
                          algorithm="RS256", headers={"kid": "issuer-key"})
    forms = []
    def identity(request):
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
                                           "token_endpoint": issuer + "/token", "jwks_uri": issuer + "/keys"})
        if path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [jwk]})
        if path.endswith("/token"):
            form = parse_qs(request.content.decode())
            forms.append(form)
            cid = form["client_id"][0]
            if cid == "browser-client":
                return httpx.Response(200, json={"id_token": sign(state["user"], cid, nonce=state["owner_nonce"])})
            assert "client_secret" not in form
            assert form.get("resource") == [broker + "/mcp"]
            if form["grant_type"] == ["authorization_code"]:
                expected = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
                assert expected == state["challenge"]
            else:
                if state["refresh_denied"]:
                    return httpx.Response(400, json={"error": "invalid_grant", "private": "do-not-expose"})
                state["rotation"] += 1
            subject = state["user"]
            payload = {"token_type": "Bearer", "access_token": sign(subject, broker + "/mcp", scope="auth:read auth:connect auth:use auth:disconnect"),
                       "refresh_token": "platform-refresh-" + str(state["rotation"]), "expires_in": 300,
                       "id_token": sign(subject, state.get("id_audience", "workswarm-desktop"),
                                        nonce=state.get("id_nonce", state["nonce"]))}
            return httpx.Response(200, json=payload)
        raise AssertionError("unexpected issuer request")
    app = create_app(settings, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: identity(req) if req.url.host == "identity.example" else vendor(req))))
    with TestClient(app, base_url=broker) as api:
        def transport(request):
            if request.url.host == "identity.example":
                return identity(request)
            assert request.url.host == "broker.example"
            if state["identity_offline"] and request.url.path == "/api/auth/session":
                return httpx.Response(503, json={"error": "temporary_unavailable"})
            return api.request(request.method, str(request.url), content=request.content,
                               headers=dict(request.headers), follow_redirects=False)
        def opener(url):
            values = parse_qs(urlsplit(url).query)
            state.update(nonce=values["nonce"][0], challenge=values["code_challenge"][0])
            # Exercise the real loopback HTTP server; an unrelated request cannot consume it.
            response = httpx.get(values["redirect_uri"][0], params={"state": "wrong", "code": "wrong"}, trust_env=False)
            assert response.status_code == 400
            response = httpx.get(values["redirect_uri"][0], params={"state": values["state"][0], "code": "one-use-code"}, trust_env=False)
            assert response.status_code == 200
            return True
        clients = []
        def desktop(user, root=None, login=True):
            state["user"] = user
            profile = CloudProfile(broker, issuer)
            client = CloudClient(root or tmp_path / user, profile, client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(transport), follow_redirects=False), opener=opener)
            clients.append(client)
            if login:
                client.login(callback_port=0)
            return client
        def consent(started):
            response = api.get(started["connect_url"], follow_redirects=False)
            query = parse_qs(urlsplit(response.headers["location"]).query)
            state["owner_nonce"] = query["nonce"][0]
            response = api.get("/oauth/owner/callback", params={"state": query["state"][0], "code": "owner-code"}, follow_redirects=False)
            assert response.status_code == 302, response.text
            query = parse_qs(urlsplit(response.headers["location"]).query)
            return api.get("/oauth/providers/" + started["provider"] + "/callback",
                           params={"state": query["state"][0], "code": "vendor-code"})
        try:
            yield desktop, consent, api, app, vendor, state, forms, sign
        finally:
            for client in clients:
                client.close()


def test_desktop_login_provider_consent_and_real_api_paths(cloud_rig):
    desktop, consent, _, app, vendor, _, forms, _ = cloud_rig
    client = desktop("alice")
    assert not (client.root / "providers.json").exists()
    assert client.request("GET", "/api/auth/providers")["providers"][0]["configured"]
    started = client.request("POST", "/api/auth/connections", json={"provider": "gmail", "connection_id": "gmail-one"})
    assert consent(started).status_code == 200
    assert wait_for_connection(client, started, 2)["validation"] == "passed"
    result = client.request("POST", "/api/auth/connections/gmail-one/operations", json={"operation": "gmail_search"})
    assert result["success"]
    # Only the publisher's server exchanged the vendor code with a vendor secret.
    vendor_form = parse_qs(next(r.content.decode() for r in vendor.requests if r.method == "POST"))
    assert vendor_form["client_secret"] == ["server-only-client-secret"]
    assert all("client_secret" not in form for form in forms)
    assert b"platform-refresh" not in client.session_path.read_bytes()
    assert b"server-only-client-secret" not in client.session_path.read_bytes()


def test_two_platform_users_cannot_access_same_named_connection(cloud_rig):
    desktop, consent, *_ = cloud_rig
    alice = desktop("alice")
    started = alice.request("POST", "/api/auth/connections", json={"provider": "github", "connection_id": "same-name"})
    assert consent(started).status_code == 200
    bob = desktop("bob")
    with pytest.raises(AuthError, match="connection_not_found"):
        bob.request("GET", "/api/auth/connections/same-name")
    started = bob.request("POST", "/api/auth/connections", json={"provider": "github", "connection_id": "same-name"})
    assert consent(started).status_code == 200
    assert alice.request("GET", "/api/auth/connections/same-name")["account"] != bob.request("GET", "/api/auth/connections/same-name")["account"]


def test_unknown_subject_enrolls_only_with_signed_group(cloud_rig):
    desktop, _, api, app, _, state, _, sign = cloud_rig
    desktop("new-subject")
    state["group"] = False
    for audience in (app.state.manager.settings.origin + "/mcp", "wrong-resource"):
        token = sign("another-new-user", audience, scope="auth:read")
        assert api.get("/api/auth/providers", headers={"Authorization": "Bearer " + token}).status_code == 401
    with pytest.raises(AuthError, match="cloud_session_verification_pending"):
        desktop("not-in-group")


def test_rotated_platform_token_is_preserved_but_cannot_bypass_identity(cloud_rig):
    desktop, _, _, _, _, state, *_ = cloud_rig
    client = desktop("alice")
    tokens = client._read()
    tokens["expires_at"] = 0
    client._save(tokens)
    state["identity_offline"] = True
    with pytest.raises(AuthError, match="cloud_session_verification_pending"):
        client.request("GET", "/api/auth/providers")
    assert client._read()["refresh_token"] == "platform-refresh-1"
    assert client._read()["identity_pending"]
    with pytest.raises(AuthError, match="cloud_session_verification_pending"):
        client.request("GET", "/api/auth/providers")
    assert state["rotation"] == 1
    state["identity_offline"] = False
    assert client.request("GET", "/api/auth/providers")["success"]
    assert client._read()["identity_pending"] is False


def test_refresh_account_change_cannot_rebind_local_session(cloud_rig):
    desktop, _, _, _, _, state, *_ = cloud_rig
    client = desktop("alice")
    tokens = client._read()
    tokens["expires_at"] = 0
    client._save(tokens)
    state["user"] = "bob"
    with pytest.raises(AuthError, match="cloud_account_mismatch"):
        client.access_token()
    assert not client.session_path.exists()


def test_login_different_platform_user_requires_explicit_logout(cloud_rig):
    desktop, *_ = cloud_rig
    alice = desktop("alice")
    original = alice.session_path.read_bytes()
    with pytest.raises(AuthError, match="cloud_account_switch_requires_logout"):
        desktop("bob", root=alice.root)
    assert alice.session_path.read_bytes() == original


def test_explicit_refresh_revocation_clears_only_local_session(cloud_rig):
    desktop, _, _, app, _, state, *_ = cloud_rig
    client = desktop("alice")
    tokens = client._read()
    tokens["expires_at"] = 0
    client._save(tokens)
    state["refresh_denied"] = True
    with pytest.raises(AuthError, match="workswarm_login_required"):
        client.access_token()
    assert not client.session_path.exists()


def test_profile_change_does_not_reuse_a_platform_token(cloud_rig):
    desktop, *_ = cloud_rig
    client = desktop("alice")
    client.profile = replace(client.profile, server_url="https://different-broker.example")
    with pytest.raises(AuthError, match="workswarm_login_required"):
        client.access_token()


def test_invalid_profile_cannot_add_secrets_or_non_https(tmp_path):
    for extra in ({"client_secret": "private"}, {"server_url": "http://broker.example"},
                  {"issuer": "https://id.example?unsafe=1"}, {"callback_port": True}):
        value = {"server_url": "https://broker.example", "issuer": "https://id.example", **extra}
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="invalid_cloud_profile"):
            CloudProfile.load(path)


def test_browser_connect_link_must_target_configured_broker(cloud_rig):
    desktop, *_ = cloud_rig
    client = desktop("alice")
    for url in ("https://attacker.example/connect/x", "https://broker.example@attacker.example/connect/x",
                "https://broker.example/connect/x?token=secret", "http://broker.example/connect/x"):
        with pytest.raises(AuthError, match="cloud_invalid_connect_url"):
            client.open_connect_url(url)


def test_waiter_does_not_accept_completion_of_another_attempt(cloud_rig):
    desktop, consent, *_ = cloud_rig
    client = desktop("alice")
    one = client.request("POST", "/api/auth/connections", json={"provider": "github", "connection_id": "one"})
    two = client.request("POST", "/api/auth/connections", json={"provider": "github", "connection_id": "one"})
    assert one["revision"] == two["revision"] and one["attempt_id"] != two["attempt_id"]
    assert consent(two).status_code == 200
    with pytest.raises(AuthError, match="authorization_attempt_superseded"):
        wait_for_connection(client, one, 2)
    assert wait_for_connection(client, two, 2)["validation"] == "passed"


def test_cancel_only_current_attempt_preserves_old_grant(rig):
    authorize(rig)
    client, app, *_ = rig
    before = app.state.vault.credentials(owner_id("local"), "account-one")
    params, _ = begin(rig)
    info = client.get("/api/auth/connections/account-one", headers=headers()).json()
    pending = info["pending_attempt_id"]
    assert client.post("/api/auth/connections/account-one/cancel", headers=headers("alice"), json={"attempt_id": pending}).status_code == 404
    assert client.post("/api/auth/connections/account-one/cancel", headers=headers(), json={"attempt_id": "wrong"}).status_code == 409
    assert client.post("/api/auth/connections/account-one/cancel", headers=headers(), json={"attempt_id": pending}).status_code == 200
    assert app.state.vault.credentials(owner_id("local"), "account-one") == before
    assert client.get("/oauth/providers/gmail/callback", params={"state": params["state"][0], "code": "late"}).status_code == 409


def test_timeout_cancels_its_attempt_without_erasing_prior_credentials(cloud_rig):
    desktop, consent, *_ = cloud_rig
    client = desktop("alice")
    first = client.request("POST", "/api/auth/connections", json={"provider": "github", "connection_id": "one"})
    assert consent(first).status_code == 200
    second = client.request("POST", "/api/auth/connections", json={"provider": "github", "connection_id": "one"})
    with pytest.raises(AuthError, match="authorization_timeout"):
        wait_for_connection(client, second, 0)
    assert client.request("GET", "/api/auth/connections/one")["authorization"] == "authorized"
    assert client.request("GET", "/api/auth/connections/one")["pending"] is False


def test_cloud_mcp_protocol_tools_use_actual_broker_without_provider_config(cloud_rig):
    desktop, consent, *_ = cloud_rig
    client = desktop("alice")
    client.opener = lambda url: consent(client._started).status_code == 200 if hasattr(client, "_started") else True
    original = client.request
    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == "POST" and path == "/api/auth/connections":
            client._started = result
        return result
    client.request = request
    mcp = build_cloud_mcp(client.root, cloud_client=client)
    async def scenario():
        tools = {tool.name for tool in await mcp.list_tools()}
        assert tools == {"auth_providers", "auth_login", "auth_connect", "auth_status", "auth_validate", "service_call", "auth_disconnect", "auth_cancel"}
        async def call(name, arguments):
            result = await mcp.call_tool(name, arguments)
            if isinstance(result, tuple):
                result = result[0]
            return json.loads(result[0].text)
        started = await call("auth_connect", {"provider": "github", "connection_id": "mcp-one"})
        assert started["browser_opened"]
        assert (await call("auth_validate", {"connection_id": "mcp-one"}))["validation"] == "passed"
        assert (await call("service_call", {"connection_id": "mcp-one", "operation": "github_profile"}))["success"]
    asyncio.run(scenario())


def test_publisher_render_enables_requested_providers_with_their_operations(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from publisher_setup import render
    from workswarm_auth.providers import DEFINITIONS
    rendered = tmp_path / "publisher-runtime"
    render(rendered, "https://auth.example.com", "https://login.example.com/realms/workswarm", "login.example.com",
           providers=tuple(DEFINITIONS))
    remote = json.loads((rendered / "config/remote.json").read_text())
    assert remote["enrollment"]["providers"] == list(DEFINITIONS)
    assert remote["enrollment"]["operations"] == [op for d in DEFINITIONS.values() for op in d.operations]
    providers = json.loads((rendered / "config/providers.json").read_text())
    assert providers["microsoft"]["tenant"] == "common"
    assert providers["slack"]["pkce"] is False and providers["slack"]["token_kind"] == "bot"
    assert providers["google-drive"]["redirect_uri"] == "https://auth.example.com/oauth/providers/google-drive/callback"
    # The rendered runtime must load as-is.
    from workswarm_auth.config import load_settings
    assert set(load_settings(rendered / "config").providers) == set(DEFINITIONS)
    with pytest.raises(ValueError, match="publisher_providers_must_be_distinct_known_ids"):
        render(tmp_path / "other", "https://auth.example.com", "https://login.example.com/realms/workswarm",
               "login.example.com", providers=("gmail", "dropbox"))


def test_publisher_render_and_release_contain_no_server_secrets(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from publisher_setup import render
    from build_cloud_client import build
    source = Path(__file__).resolve().parents[1]
    rendered = tmp_path / "publisher-runtime"
    render(rendered, "https://auth.example.com", "https://login.example.com/realms/workswarm", "login.example.com")
    remote = json.loads((rendered / "config/remote.json").read_text())
    assert remote["enrollment"]["group"] == "workswarm-users"
    realm = json.loads((rendered / "realm.json").read_text())
    assert all(item["publicClient"] and item["attributes"]["pkce.code.challenge.method"] == "S256" for item in realm["clients"])
    # Every default scope must be defined in the import, and access tokens must carry `sub`.
    defined = {item["name"]: item for item in realm["clientScopes"]}
    assert all(set(item["defaultClientScopes"]) <= set(defined) for item in realm["clients"])
    assert all("basic" in item["defaultClientScopes"] for item in realm["clients"])
    assert [m["protocolMapper"] for m in defined["basic"]["protocolMappers"]] == ["oidc-sub-mapper"]
    assert (rendered / "realm.json").stat().st_mode & 0o777 == 0o644
    assert (rendered / "secrets/vault.key").stat().st_mode & 0o777 == 0o600
    result = build(source, rendered / "cloud.json", tmp_path / "release")
    with zipfile.ZipFile(result["market_zip"]) as archive:
        manifest = json.loads(archive.read("workswarm-auth-cloud/manifest.json"))
    for field in ("display_name", "display_description"):
        assert set(manifest[field]) == {"zh", "en"} and all(manifest[field].values())
    # The market launcher is the bootstrap, pinned to this release and the publisher origin.
    with zipfile.ZipFile(result["market_zip"]) as archive:
        server = json.loads(archive.read("workswarm-auth-cloud/mcp.json"))["mcpServers"]["workswarm-auth-cloud"]
    assert server["command"] == "py" and server["args"][:2] == ["-3", "-c"]
    code = base64.b64decode(server["args"][2].split("'")[1]).decode()
    assert 'VERSION = "' + workswarm_auth.__version__ + '"' in code and 'ORIGIN = "https://auth.example.com"' in code
    compile(code, "bootstrap", "exec")
    # The wheel carries only client modules plus the public profile, with a valid RECORD.
    with zipfile.ZipFile(result["client_wheel"]) as archive:
        names = set(archive.namelist())
        assert json.loads(archive.read("workswarm_auth/publisher_profile.json"))["server_url"] == "https://auth.example.com"
        record = archive.read(f"workswarm_auth-{workswarm_auth.__version__}.dist-info/RECORD").decode().splitlines()
        for line in record:
            name, digest, _ = line.rsplit(",", 2)
            if digest:
                expected = base64.urlsafe_b64encode(hashlib.sha256(archive.read(name)).digest()).rstrip(b"=").decode()
                assert digest == "sha256=" + expected
    assert not names & {f"workswarm_auth/{m}" for m in ("app.py", "config.py", "vault.py", "providers.py", "manager.py", "security.py")}
    for name in ("client_wheel", "market_zip"):
        with zipfile.ZipFile(result[name]) as archive:
            paths = archive.namelist()
            assert not any(Path(p).name in {"providers.json", "remote.json", "config.env", ".env", "vault.key", "vault.sqlite3"} for p in paths)
            data = b"".join(archive.read(p) for p in paths)
            assert (rendered / "secrets/vault.key").read_bytes() not in data
            assert b"server-only-client-secret" not in data
    with pytest.raises(ValueError, match="cloud_release_exists"):
        build(source, rendered / "cloud.json", tmp_path / "release")


@pytest.mark.parametrize("field,value", [("id_nonce", "another-login"), ("id_audience", "another-client")])
def test_desktop_rejects_wrong_id_token_nonce_or_audience(cloud_rig, field, value):
    desktop, _, _, _, _, state, *_ = cloud_rig
    state[field] = value
    with pytest.raises(AuthError, match="cloud_identity_invalid"):
        desktop("alice")


def test_mcp_first_connect_performs_platform_login_automatically(cloud_rig):
    desktop, consent, *_ = cloud_rig
    client = desktop("alice", login=False)
    # Do not depend on a well-known local port being available in a test environment.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client.profile = replace(client.profile, callback_port=port)
    platform_opener = client.opener
    original = client.request
    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == "POST" and path == "/api/auth/connections":
            client._started = result
        return result
    client.request = request
    client.opener = lambda url: (platform_opener(url) if urlsplit(url).hostname == "identity.example"
                                else consent(client._started).status_code == 200)
    mcp = build_cloud_mcp(client.root, cloud_client=client)
    async def scenario():
        result = await mcp.call_tool("auth_connect", {"provider": "github", "connection_id": "first-connect"})
        if isinstance(result, tuple):
            result = result[0]
        assert json.loads(result[0].text)["browser_opened"]
    asyncio.run(scenario())
    assert client._read()["subject"] == "alice"
    assert not (client.root / "providers.json").exists()


def test_auth_login_restores_a_lost_session_without_starting_provider_authorization(cloud_rig):
    desktop, _, _, _, _, state, *_ = cloud_rig
    client = desktop("alice", login=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client.profile = replace(client.profile, callback_port=port)
    platform_opener = client.opener
    opened = []
    client.opener = lambda url: opened.append(url) or platform_opener(url)
    mcp = build_cloud_mcp(client.root, cloud_client=client)
    async def call(name, arguments=None):
        result = await mcp.call_tool(name, arguments or {})
        if isinstance(result, tuple):
            result = result[0]
        return json.loads(result[0].text)
    async def scenario():
        # Reading never opens a browser; the error tells the agent how to recover.
        blocked = await call("auth_status")
        assert blocked["error"] == "workswarm_login_required" and "auth_login" in blocked["instruction"]
        assert opened == []
        assert await call("auth_login") == {"success": True, "authenticated": True}
        assert len(opened) == 1
        assert await call("auth_login") == {"success": True, "authenticated": True, "already_signed_in": True}
        assert len(opened) == 1
        assert "error" not in await call("auth_status")
        # An expired session whose refresh token was revoked recovers the same way.
        tokens = client._read()
        tokens["expires_at"] = 0
        client._save(tokens)
        state["refresh_denied"] = True
        assert (await call("auth_status"))["error"] == "workswarm_login_required"
        assert not client.session_path.exists()
        assert await call("auth_login") == {"success": True, "authenticated": True}
        assert len(opened) == 2
    asyncio.run(scenario())
    assert client._read()["subject"] == "alice"
    assert all(urlsplit(url).hostname == "identity.example" for url in opened)


def test_service_call_with_a_wrong_operation_name_lists_the_usable_ones(cloud_rig):
    desktop, consent, *_ = cloud_rig
    client = desktop("alice")
    client.opener = lambda url: True
    started = client.request("POST", "/api/auth/connections", json={"provider": "github", "connection_id": "mcp-ops", "label": ""})
    assert consent(started).status_code == 200
    mcp = build_cloud_mcp(client.root, cloud_client=client)
    async def call(name, arguments):
        result = await mcp.call_tool(name, arguments)
        if isinstance(result, tuple):
            result = result[0]
        return json.loads(result[0].text)
    async def scenario():
        # Unknown everywhere (policy), and known but belonging to another provider (adapter).
        for operation, code in (("list_messages", "operation_denied_by_policy"), ("gmail_search", "operation_not_allowed")):
            denied = await call("service_call", {"connection_id": "mcp-ops", "operation": operation})
            assert denied["error"] == code and denied["success"] is False
            assert denied["allowed_operations"] == ["github_profile", "github_repos"]
            assert "allowed_operations" in denied["instruction"]
        assert "allowed_operations" in next(t for t in await mcp.list_tools() if t.name == "service_call").description
        assert (await call("service_call", {"connection_id": "mcp-ops", "operation": "github_profile"}))["success"]
        # The refusal is still reported when the list cannot be fetched.
        original = client.request
        def no_listing(method, path, **kwargs):
            if method == "GET":
                raise AuthError("cloud_identity_unavailable", 502)
            return original(method, path, **kwargs)
        client.request = no_listing
        denied = await call("service_call", {"connection_id": "mcp-ops", "operation": "list_messages"})
        assert denied["error"] == "operation_denied_by_policy" and "instruction" in denied and "allowed_operations" not in denied
    asyncio.run(scenario())


def test_auth_login_reports_a_host_without_a_browser(cloud_rig):
    desktop, *_ = cloud_rig
    client = desktop("alice", login=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client.profile = replace(client.profile, callback_port=port)
    client.opener = lambda url: False
    mcp = build_cloud_mcp(client.root, cloud_client=client)
    async def scenario():
        result = await mcp.call_tool("auth_login", {})
        if isinstance(result, tuple):
            result = result[0]
        return json.loads(result[0].text)
    assert asyncio.run(scenario())["error"] == "cloud_browser_unavailable_run_cloud_login"
    assert not client.session_path.exists()


def test_real_stdio_cloud_client_starts_without_vendor_config(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    profile = CloudProfile("https://broker.example", "https://issuer.example")
    (tmp_path / "cloud.json").write_text(json.dumps(profile.binding))
    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=["-m", "workswarm_auth.cli", "cloud-mcp", "--home", str(tmp_path)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 8
                result = await session.call_tool("auth_providers", {})
                value = json.loads(result.content[0].text)
                assert value["error"] == "workswarm_login_required"
                assert "auth_login" in value["instruction"]
    asyncio.run(scenario())
    assert not (tmp_path / "providers.json").exists()


def test_deployment_reads_separate_publisher_config_without_mutating_it(tmp_path, monkeypatch):
    server, runtime = tmp_path / "config", tmp_path / "data"
    server.mkdir()
    runtime.mkdir()
    remote = {"issuer": "https://id.example", "jwks_uri": "https://id.example/keys", "public_origin": "https://broker.example",
              "owner_login": {"client_id": "browser", "authorization_endpoint": "https://id.example/auth", "token_endpoint": "https://id.example/token"},
              "principals": {}, "enrollment": {"group": "members", "providers": ["github"], "operations": ["github_profile"]}}
    (server / "remote.json").write_text(json.dumps(remote))
    (server / "providers.json").write_text(json.dumps({"github": {"client_id": "publisher-app", "client_secret": "publisher-secret"}}))
    monkeypatch.setenv("WORKSWARM_REMOTE_CONFIG_FILE", str(server / "remote.json"))
    monkeypatch.setenv("WORKSWARM_PROVIDER_CONFIG_FILE", str(server / "providers.json"))
    settings = load_settings(runtime)
    assert settings.remote["enrollment"]["group"] == "members"
    assert settings.providers["github"].client_secret == "publisher-secret"
    assert list(runtime.iterdir()) == []


def test_group_enrollment_requires_an_explicit_policy(tmp_path):
    remote = {"issuer": "https://id.example", "jwks_uri": "https://id.example/keys", "public_origin": "https://broker.example",
              "owner_login": {"client_id": "browser", "authorization_endpoint": "https://id.example/auth", "token_endpoint": "https://id.example/token"},
              "principals": {}, "enrollment": {"providers": ["github"], "operations": ["github_profile"]}}
    with pytest.raises(ValueError, match="remote_enrollment_requires_group"):
        Settings(tmp_path, remote=remote)


def test_login_providers_enroll_new_users_and_keep_secrets_off_stdout(capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from configure_login_providers import admin_client, configure
    calls = []
    def handler(request):
        is_json = request.headers.get("content-type") == "application/json"
        calls.append((request.method, request.url.path, json.loads(request.content) if is_json else None))
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "g1", "name": "workswarm-users"}])
        if request.method == "GET" and request.url.path.endswith("/instances/google"):
            return httpx.Response(404)
        if request.method == "GET" and request.url.path.endswith("/instances/github"):
            return httpx.Response(200, json={"alias": "github"})
        return httpx.Response(204)
    client = admin_client("https://kc.example", "admin", "pw", httpx.Client(transport=httpx.MockTransport(handler)))
    result = configure(client, "https://kc.example", ["google", "github"],
                       {"google": ("g-id", "g-secret"), "github": ("h-id", "h-secret")}, auto_enroll=True)
    assert result == {"identity_providers": ["google", "github"], "auto_enroll_group": "workswarm-users"}
    assert ("PUT", "/admin/realms/workswarm/default-groups/g1", None) in calls
    created = next(body for method, path, body in calls if method == "POST" and path.endswith("/identity-provider/instances"))
    assert created["alias"] == "google" and created["config"]["clientSecret"] == "g-secret"
    updated = next(body for method, path, body in calls if method == "PUT" and path.endswith("/instances/github"))
    assert updated["config"]["defaultScope"] == "read:user user:email"
    assert "secret" not in capsys.readouterr().out
