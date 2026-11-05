import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from workswarm_auth.app import create_app
from workswarm_auth.config import ProviderSettings, Settings
from workswarm_auth.vault import Vault
from test_universal_auth import VendorMock, authorize, begin, headers, rig, owner_id


def test_account_binding_uses_service_subject_and_is_owner_scoped(rig):
    authorize(rig)
    client = rig[0]
    first = client.get('/api/auth/connections/account-one', headers=headers()).json()['account']
    assert first['account_id'].startswith('acct_')
    assert 'subject' not in first
    authorize(rig, 'google-drive', 'drive-one')
    assert client.get('/api/auth/connections/drive-one', headers=headers()).json()['account']['account_id'] == first['account_id']
    authorize(rig, 'gmail', 'alice-one', 'alice')
    assert client.get('/api/auth/connections/alice-one', headers=headers('alice')).json()['account']['account_id'] != first['account_id']
    assert b'google-user-1' not in rig[1].state.vault.path.read_bytes()


def test_reconnect_to_different_account_cannot_overwrite_binding(rig):
    authorize(rig)
    client, app, vendor, _ = rig
    original = client.get('/api/auth/connections/account-one', headers=headers()).json()['account']
    tokens = app.state.vault.credentials(owner_id("local"), 'account-one')
    vendor.subject = 'another-google-account'
    params, _ = begin(rig)
    response = client.get('/oauth/providers/gmail/callback', params={'state': params['state'][0], 'code': 'code'})
    assert response.status_code == 409 and response.json()['error'] == 'account_mismatch_use_new_connection'
    # The other account's grant is rejected; the pinned account keeps working.
    assert app.state.vault.credentials(owner_id("local"), 'account-one') == tokens
    assert app.state.vault.info(owner_id("local"), 'account-one')['authorization'] == 'authorized'
    assert app.state.vault.account(owner_id("local"), 'account-one') == original
    authorize(rig, 'gmail', 'account-two')
    assert app.state.vault.account(owner_id("local"), 'account-two')['account_id'] != original['account_id']


@pytest.mark.parametrize('refresh', [False, True])
def test_account_change_invalidates_credentials_before_api_use(rig, refresh):
    authorize(rig)
    client, app, vendor, _ = rig
    if refresh:
        info = app.state.vault.info(owner_id("local"), 'account-one')
        tokens = app.state.vault.credentials(owner_id("local"), 'account-one')
        tokens['expires_at'] = 0
        app.state.vault.save(owner_id("local"), 'account-one', info['revision'], tokens)
    vendor.subject = 'replaced-account'
    count = len(vendor.requests)
    result = client.post('/api/auth/connections/account-one/validate', headers=headers())
    assert result.status_code == 409
    assert app.state.vault.credentials(owner_id("local"), 'account-one') is None
    assert not any(req.url.path.endswith('/messages') for req in vendor.requests[count:])


def test_missing_identity_is_never_marked_authorized(rig):
    rig[2].subject = ''
    params, _ = begin(rig)
    response = rig[0].get('/oauth/providers/gmail/callback', params={'state': params['state'][0], 'code': 'code'})
    assert response.status_code == 401
    assert rig[1].state.vault.credentials(owner_id("local"), 'account-one') is None
    assert rig[1].state.vault.info(owner_id("local"), 'account-one')['authorization'] == 'failed'


def test_external_vault_key_is_not_regenerated_or_chmodded(tmp_path, monkeypatch):
    mounted = tmp_path / 'mounted.key'
    mounted.write_bytes(Fernet.generate_key())
    mounted.chmod(0o400)
    mode, original = mounted.stat().st_mode, mounted.read_bytes()
    monkeypatch.setenv('WORKSWARM_VAULT_KEY_FILE', str(mounted))
    vault = Vault(tmp_path / 'data')
    revision, flow = vault.begin('local', 'one', 'gmail', '')
    vault.complete('local', 'one', revision, flow, {'access_token': 'private-token'})
    assert mounted.read_bytes() == original and mounted.stat().st_mode == mode
    assert not (tmp_path / 'data' / 'vault.key').exists()
    assert Vault(tmp_path / 'data').credentials('local', 'one')['access_token'] == 'private-token'
    monkeypatch.setenv('WORKSWARM_VAULT_KEY_FILE', str(tmp_path / 'missing.key'))
    with pytest.raises(RuntimeError):
        Vault(tmp_path / 'other')


def test_v03_vault_gains_flow_columns_and_keeps_credentials(tmp_path):
    import sqlite3
    key = Fernet.generate_key()
    (tmp_path / 'vault.key').write_bytes(key)
    with sqlite3.connect(tmp_path / 'vault.sqlite3') as db:
        db.execute("""CREATE TABLE connections (owner TEXT NOT NULL, cid TEXT NOT NULL, provider TEXT NOT NULL,
            label TEXT NOT NULL, revision INTEGER NOT NULL, tokens BLOB, authorization TEXT NOT NULL,
            validation TEXT NOT NULL, checked_at REAL, last_error TEXT, PRIMARY KEY(owner,cid))""")
        db.execute("INSERT INTO connections VALUES ('local','gmail-default','gmail','',3,?,'authorized','passed',1,NULL)",
                   (Fernet(key).encrypt(b'{"access_token": "private-token"}'),))
    db.close()
    vault = Vault(tmp_path)
    assert vault.info('local', 'gmail-default')['pending'] is False
    assert vault.credentials('local', 'gmail-default')['access_token'] == 'private-token'
    revision, flow = vault.begin('local', 'gmail-default', 'gmail', '')
    assert revision == 3 and vault.complete('local', 'gmail-default', revision, flow, {'access_token': 'new'})


@pytest.fixture
def remote(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='test-key', alg='RS256', use='sig')
    policy = {'providers': ['github'], 'operations': ['github_profile']}
    config = {'issuer': 'https://issuer.example', 'jwks_uri': 'https://issuer.example/keys',
              'public_origin': 'https://broker.example', 'principals': {'alice': policy, 'bob': policy},
              'owner_login': {'client_id': 'browser-client', 'authorization_endpoint': 'https://issuer.example/authorize',
                              'token_endpoint': 'https://issuer.example/token'}}
    settings = Settings(tmp_path, {'github': ProviderSettings('client-github', 'secret')}, remote=config)
    vendor = VendorMock()
    idp = {}
    def sign(claims):
        return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'test-key'})
    def handler(request):
        if request.url.host == 'issuer.example' and request.url.path == '/token':
            # The browser user who signed in at the issuer, bound to the login request's nonce.
            return httpx.Response(200, json={'id_token': sign({'iss': config['issuer'], 'sub': idp['subject'], 'aud': 'browser-client',
                                                                'exp': int(time.time()) + 300, 'nonce': idp['nonce']})})
        if request.url.host == 'issuer.example':
            return httpx.Response(200, json={'keys': [jwk]})
        return vendor(request)
    app = create_app(settings, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    def token(subject='alice', scope='auth:read auth:connect auth:use auth:disconnect', **overrides):
        claims = {'iss': config['issuer'], 'sub': subject, 'aud': config['public_origin'] + '/mcp',
                  'exp': int(time.time()) + 300, 'scope': scope}
        claims.update(overrides)
        return sign(claims)
    with TestClient(app, base_url=config['public_origin']) as client:
        def open_link(link, subject, forward_sign_in=False):
            """Open a connect link in a browser that signs in to the issuer as `subject`.

            forward_sign_in: the signed-in callback URL is instead opened by another browser.
            """
            login = client.get(link, follow_redirects=False)
            assert login.status_code == 302 and login.headers['location'].startswith('https://issuer.example/authorize?')
            query = parse_qs(urlsplit(login.headers['location']).query)
            idp.update(subject=subject, nonce=query['nonce'][0])
            if forward_sign_in:
                client.cookies.clear()
            return client.get('/oauth/owner/callback', params={'state': query['state'][0], 'code': 'idp-code'}, follow_redirects=False)
        yield client, app, token, open_link


def bearer(value):
    return {'Authorization': 'Bearer ' + value}


@pytest.mark.parametrize('overrides', [
    {'aud': 'https://other.example/mcp'}, {'iss': 'https://evil.example'}, {'exp': 1},
    {'sub': 'unapproved-user'}, {'sub': ''}, {'scope': ['auth:read']},
])
def test_remote_rejects_wrong_audience_issuer_expiry_subject_and_claim_shape(remote, overrides):
    client, _, token, _ = remote
    response = client.get('/api/auth/providers', headers=bearer(token(**overrides)))
    assert response.status_code == 401
    assert response.headers['www-authenticate'] == 'Bearer'


def test_remote_rejects_static_key_unsigned_and_forged_signature(remote):
    client, _, token, _ = remote
    value = token()
    claims = jwt.decode(value, options={'verify_signature': False})
    forged = jwt.encode(claims, 'fake-shared-key-that-is-not-used-by-the-server', algorithm='HS256', headers={'kid': 'test-key'})
    unsigned = jwt.encode(claims, '', algorithm='none', headers={'kid': 'test-key'})
    for candidate in ['x' * 48, forged, unsigned, value[:-8] + 'tampered']:
        assert client.get('/api/auth/providers', headers=bearer(candidate)).status_code == 401


def test_remote_scopes_operation_policy_owner_isolation_and_redacted_audit(remote):
    client, app, token, open_link = remote
    alice, bob = bearer(token()), bearer(token('bob'))
    denied = client.post('/api/auth/connections', headers=bearer(token(scope='auth:read')),
                         json={'provider': 'github', 'connection_id': 'one'})
    assert denied.status_code == 403 and denied.json()['error'] == 'insufficient_scope'
    assert client.post('/api/auth/connections', headers=alice, json={'provider': 'gmail'}).status_code == 403
    started = client.post('/api/auth/connections', headers=alice, json={'provider': 'github', 'connection_id': 'one', 'owner': 'bob'}).json()
    redirect = open_link(started['connect_url'], 'alice')
    state = parse_qs(urlsplit(redirect.headers['location']).query)['state'][0]
    assert client.get('/oauth/providers/github/callback', params={'state': state, 'code': 'secret-auth-code'}).status_code == 200
    assert client.get('/api/auth/connections/one', headers=bob).status_code == 404
    assert client.post('/api/auth/connections/one/operations', headers=alice, json={'operation': 'github_repos'}).status_code == 403
    assert client.post('/api/auth/connections/one/validate', headers=alice).status_code == 200
    history = client.get('/api/auth/audit', headers=alice).text
    assert 'operation_denied_by_policy' in history and 'validate' in history
    assert not any(private in history for private in ('private-access', 'secret-auth-code', token(), 'private-client-secret'))
    assert client.get('/api/auth/connections', headers=bob).json()['connections'] == []
    owner = app.state.manager.vault.list(next(iter(app.state.manager.locks))[0])[0]['owner']
    assert owner.startswith('oidc_') and owner != 'bob'


def test_remote_connect_link_only_works_for_its_owner(remote):
    client, app, token, open_link = remote
    started = client.post('/api/auth/connections', headers=bearer(token()), json={'provider': 'github', 'connection_id': 'one'}).json()
    # Alice forwards her link; Bob signs in and must not be able to attach his GitHub account to it.
    response = open_link(started['connect_url'], 'bob')
    assert response.status_code == 403 and response.json()['error'] == 'connect_link_belongs_to_another_user'
    assert client.get(started['connect_url'], follow_redirects=False).json()['error'] == 'connection_link_expired_or_used'
    status = client.get('/api/auth/connections/one', headers=bearer(token())).json()
    assert status['authorization'] == 'pending' and status['credential_present'] is False


def test_remote_forwarded_vendor_url_cannot_attach_another_browsers_account(remote):
    client, _, token, open_link = remote
    started = client.post('/api/auth/connections', headers=bearer(token()), json={'provider': 'github', 'connection_id': 'one'}).json()
    redirect = open_link(started['connect_url'], 'alice')
    state = parse_qs(urlsplit(redirect.headers['location']).query)['state'][0]
    # Alice passes the GitHub consent URL on; Bob's browser never opened her link.
    client.cookies.clear()
    response = client.get('/oauth/providers/github/callback', params={'state': state, 'code': 'bobs-code'})
    assert response.status_code == 403 and response.json()['error'] == 'browser_binding_mismatch'
    status = client.get('/api/auth/connections/one', headers=bearer(token())).json()
    assert status['credential_present'] is False and status['account'] is None


def test_remote_forwarded_sign_in_callback_is_rejected_before_code_redemption(remote):
    client, _, token, open_link = remote
    started = client.post('/api/auth/connections', headers=bearer(token()), json={'provider': 'github', 'connection_id': 'one'}).json()
    # Alice signs in herself, then sends the issuer's callback URL to someone else.
    response = open_link(started['connect_url'], 'alice', forward_sign_in=True)
    assert response.status_code == 403 and response.json()['error'] == 'browser_binding_mismatch'


def test_forwarded_vendor_url_is_rejected_for_oidc_user(rig):
    params, _ = begin(rig)
    client, app, _, _ = rig
    client.cookies.clear()
    response = client.get('/oauth/providers/gmail/callback', params={'state': params['state'][0], 'code': 'code'})
    assert response.status_code == 403 and response.json()['error'] == 'browser_binding_mismatch'
    assert app.state.vault.credentials(owner_id("local"), 'account-one') is None


def test_remote_requires_owner_login_configuration(tmp_path):
    config = {'issuer': 'https://issuer.example', 'jwks_uri': 'https://issuer.example/keys',
              'public_origin': 'https://broker.example', 'principals': {'alice': {'providers': [], 'operations': []}}}
    with pytest.raises(ValueError, match='remote_requires_owner_login_client'):
        Settings(tmp_path, remote=config)
    config['owner_login'] = {'client_id': 'browser-client', 'authorization_endpoint': 'http://issuer.example/authorize',
                             'token_endpoint': 'https://issuer.example/token'}
    with pytest.raises(ValueError, match='remote_urls_require_https'):
        Settings(tmp_path, remote=config)


def test_untrusted_validation_input_is_not_echoed_into_errors(rig):
    response = rig[0].post('/api/auth/connections', headers=headers(), json={'provider': {'unexpected': 'private-secret-value'}})
    assert response.status_code == 422
    assert response.json() == {'success': False, 'error': 'invalid_request'}
    assert 'private-secret-value' not in response.text




def test_client_wheel_download_is_public_and_limited_to_the_wheel(remote, tmp_path):
    client = remote[0]
    (tmp_path / "client").mkdir()
    (tmp_path / "client" / "workswarm_auth-0.7.0-py3-none-any.whl").write_bytes(b"wheel-bytes")
    (tmp_path / "client" / "notes.txt").write_text("not served")
    (tmp_path / "vault.sqlite3").touch()
    response = client.get("/client/workswarm_auth-0.7.0-py3-none-any.whl")
    assert response.status_code == 200 and response.content == b"wheel-bytes"
    for name in ("notes.txt", "workswarm_auth-9.9.9-py3-none-any.whl", "..%2Fvault.sqlite3", "workswarm_auth-0.7.0-py3-none-any.whl.bak"):
        assert client.get("/client/" + name).status_code == 404
