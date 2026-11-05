"""Public desktop OIDC client and credential-free-of-vendor-secrets cloud bridge.

Only the user's own platform session is stored locally. Provider client secrets,
provider tokens and user account pins stay on the publisher's broker.
"""
import base64
import hashlib
import json
import math
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from .errors import AuthError
from .session_store import SessionStore, session_lock


def secure_url(value, *, origin=False):
    if not isinstance(value, str) or any(ord(c) < 33 or c in "\\{}" for c in value):
        raise ValueError("cloud_urls_require_https")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or value.endswith("/") or (origin and url.path)):
        raise ValueError("cloud_urls_require_https")
    try:
        url.port
    except ValueError:
        raise ValueError("cloud_urls_require_https") from None
    return value


@dataclass(frozen=True)
class CloudProfile:
    server_url: str
    issuer: str
    client_id: str = "workswarm-desktop"
    callback_port: int = 18766

    def __post_init__(self):
        secure_url(self.server_url, origin=True)
        secure_url(self.issuer)
        if (not isinstance(self.client_id, str) or not self.client_id or len(self.client_id) > 200
                or any(ord(c) < 33 for c in self.client_id)):
            raise ValueError("invalid_cloud_client_id")
        if type(self.callback_port) is not int or not 1024 <= self.callback_port <= 65535:
            raise ValueError("invalid_cloud_callback_port")

    @classmethod
    def load(cls, path):
        try:
            return cls(**json.loads(Path(path).read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError):
            raise ValueError("invalid_cloud_profile") from None

    @property
    def resource(self):
        return self.server_url + "/mcp"

    @property
    def binding(self):
        return asdict(self)


class LoopbackLogin:
    """One loopback receiver; reject wrong states without terminating a valid login.

    The callback is deliberately NOT the provider callback; this receives only
    the platform's one-use authorization code, bound by PKCE and OIDC nonce.
    """
    def __init__(self, port, state):
        self.state, self.code, self.error = state, None, None
        self.done = threading.Event()
        login = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # URLs contain authorization codes; never log them.

            def do_GET(self):
                url = urlsplit(self.path)
                values = parse_qs(url.query)
                host = self.headers.get("Host", "")
                valid = (url.path == "/oauth/callback" and host == f"127.0.0.1:{login.port}"
                         and len(values.get("state", [])) == 1
                         and secrets.compare_digest(values["state"][0].encode(), login.state.encode()))
                if not valid:
                    self.send_error(400, "Invalid authorization callback")
                    return
                with login.guard:
                    if login.done.is_set():
                        self.send_error(409, "Authorization already completed")
                        return
                    code, errors = values.get("code", []), values.get("error", [])
                    if len(code) == 1 and 0 < len(code[0]) <= 4096 and not errors:
                        login.code = code[0]
                    else:
                        login.error = "cloud_login_denied"
                    login.done.set()
                body = b"Authorization received. Return to WorkSwarm."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.guard = threading.Lock()
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Callback)
        except OSError:
            raise AuthError("cloud_login_callback_port_in_use", 409) from None
        self.port = self.server.server_address[1]
        self.server.daemon_threads = True

    @property
    def redirect_uri(self):
        return f"http://127.0.0.1:{self.port}/oauth/callback"

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class CloudClient:
    def __init__(self, root, profile=None, *, client_factory=None, opener=None):
        self.root = Path(root)
        self.profile = profile or CloudProfile.load(self.root / "cloud.json")
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=35, trust_env=False, follow_redirects=False))
        self.opener = opener or (lambda url: webbrowser.open(url, new=2))
        self.sessions = SessionStore(self.root / "cloud-session")
        self.session_path = self.sessions.path
        self.stop_event = threading.Event()
        self.background_jobs = []

    def close(self):
        self.stop_event.set()
        for thread in self.background_jobs:
            if thread is not threading.current_thread():
                thread.join(timeout=3)

    def _read(self):
        return self.sessions.read(self.profile.binding)

    def _save(self, tokens):
        self.sessions.save(self.profile.binding, tokens)

    @staticmethod
    def _json(response):
        try:
            value = response.json()
        except ValueError:
            raise AuthError("cloud_invalid_response", 502) from None
        if not isinstance(value, dict):
            raise AuthError("cloud_invalid_response", 502)
        return value

    def _metadata(self, client):
        response = client.get(self.profile.issuer + "/.well-known/openid-configuration")
        if not response.is_success:
            raise AuthError("cloud_identity_unavailable", 502)
        data = self._json(response)
        if data.get("issuer") != self.profile.issuer:
            raise AuthError("cloud_issuer_mismatch", 401)
        # The publisher's issuer controls these endpoints, never model/user tool inputs.
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            try:
                endpoint = secure_url(data.get(name))
            except ValueError:
                raise AuthError("cloud_invalid_identity_metadata", 502) from None
            if urlsplit(endpoint).netloc != urlsplit(self.profile.issuer).netloc:
                raise AuthError("cloud_invalid_identity_metadata", 502)
        return data

    def _identity(self, client, metadata, encoded, nonce=None):
        try:
            header = jwt.get_unverified_header(encoded)
            if header.get("alg") not in {"RS256", "ES256"} or not isinstance(header.get("kid"), str):
                raise ValueError()
            response = client.get(metadata["jwks_uri"])
            if not response.is_success:
                raise ValueError()
            keys = self._json(response).get("keys")
            if not isinstance(keys, list) or len(keys) > 100:
                raise ValueError()
            material = next(item for item in keys if isinstance(item, dict) and item.get("kid") == header["kid"])
            if material.get("use", "sig") != "sig" or material.get("alg", header["alg"]) != header["alg"]:
                raise ValueError()
            claims = jwt.decode(encoded, jwt.PyJWK.from_dict(material, algorithm=header["alg"]).key,
                                algorithms=[header["alg"]], audience=self.profile.client_id, issuer=self.profile.issuer,
                                options={"require": ["exp", "iss", "aud", "sub"]})
            if not isinstance(claims["sub"], str) or not claims["sub"] or len(claims["sub"]) > 512:
                raise ValueError()
            if nonce is not None and claims.get("nonce") != nonce:
                raise ValueError()
            if claims.get("azp", self.profile.client_id) != self.profile.client_id:
                raise ValueError()
            if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and "azp" not in claims:
                raise ValueError()
            return claims["sub"]
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, StopIteration):
            raise AuthError("cloud_identity_invalid", 401) from None

    def _normalize(self, payload, previous=None):
        access, refresh = payload.get("access_token"), payload.get("refresh_token")
        if (not isinstance(access, str) or not access or len(access) > 65536
                or str(payload.get("token_type", "")).lower() != "bearer"
                or (refresh is not None and (not isinstance(refresh, str) or not refresh))):
            raise AuthError("cloud_invalid_token_response", 502)
        try:
            duration = float(payload["expires_in"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise AuthError("cloud_invalid_token_response", 502) from None
        return {"access_token": access, "refresh_token": refresh or (previous or {}).get("refresh_token"),
                "expires_at": time.time() + duration, "subject": (previous or {}).get("subject"),
                "identity_pending": True}

    def _confirm(self, client, tokens):
        # Cross-check the broker's signed-access-token identity against the ID token.
        response = client.get(self.profile.server_url + "/api/auth/session",
                              headers={"Authorization": "Bearer " + tokens["access_token"]})
        if not response.is_success:
            raise AuthError("cloud_session_verification_pending", response.status_code)
        expected = "oidc_" + hashlib.sha256(json.dumps([self.profile.issuer, tokens["subject"]],
                                          separators=(",", ":")).encode()).hexdigest()[:48]
        if self._json(response).get("user_id") != expected:
            raise AuthError("cloud_account_mismatch", 409)
        tokens["identity_pending"] = False

    def login(self, timeout=180, *, announce=None, callback_port=None):
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self.client_factory() as client:
            metadata = self._metadata(client)
            with LoopbackLogin(self.profile.callback_port if callback_port is None else callback_port, state) as callback:
                url = metadata["authorization_endpoint"] + "?" + urlencode({
                    "client_id": self.profile.client_id, "redirect_uri": callback.redirect_uri,
                    "response_type": "code", "scope": "openid auth:read auth:connect auth:use auth:disconnect",
                    "resource": self.profile.resource, "state": state, "nonce": nonce,
                    "code_challenge": challenge, "code_challenge_method": "S256"})
                if announce:
                    announce(url)
                opened = self.opener(url)
                if not opened and announce is None and not callback.done.is_set():
                    raise AuthError("cloud_browser_unavailable_run_cloud_login", 400)
                if not callback.done.wait(timeout):
                    raise AuthError("cloud_login_timeout", 408)
                if callback.error:
                    raise AuthError(callback.error, 400)
                response = client.post(metadata["token_endpoint"], data={
                    "grant_type": "authorization_code", "client_id": self.profile.client_id,
                    "redirect_uri": callback.redirect_uri, "code": callback.code,
                    "code_verifier": verifier, "resource": self.profile.resource})
                if not response.is_success:
                    raise AuthError("cloud_login_failed", 401)
                payload = self._json(response)
                tokens = self._normalize(payload)
                tokens["subject"] = self._identity(client, metadata, payload.get("id_token"), nonce)
                self._confirm(client, tokens)
            with session_lock(self.root / "cloud-session-lock"):
                old = self._read()
                if old and old["subject"] != tokens["subject"]:
                    raise AuthError("cloud_account_switch_requires_logout", 409)
                self._save(tokens)
        return {"success": True, "authenticated": True}

    def access_token(self, force=False):
        with session_lock(self.root / "cloud-session-lock"):
            tokens = self._read()
            if not tokens:
                raise AuthError("workswarm_login_required", 401)
            with self.client_factory() as client:
                if force or tokens["expires_at"] <= time.time() + 30:
                    if not tokens.get("refresh_token"):
                        raise AuthError("workswarm_login_required", 401)
                    metadata = self._metadata(client)
                    response = client.post(metadata["token_endpoint"], data={
                        "grant_type": "refresh_token", "client_id": self.profile.client_id,
                        "refresh_token": tokens["refresh_token"], "resource": self.profile.resource})
                    payload = self._json(response)
                    if not response.is_success:
                        if response.status_code == 400 and payload.get("error") == "invalid_grant":
                            self.session_path.unlink(missing_ok=True)
                            raise AuthError("workswarm_login_required", 401)
                        raise AuthError("cloud_identity_unavailable", 502)
                    previous = tokens
                    tokens = self._normalize(payload, previous)
                    # Persist rotation before any further network/identity checks.
                    self._save(tokens)
                    if payload.get("id_token"):
                        subject = self._identity(client, metadata, payload["id_token"])
                        if subject != previous["subject"]:
                            self.session_path.unlink(missing_ok=True)
                            raise AuthError("cloud_account_mismatch", 409)
                if tokens.get("identity_pending"):
                    self._confirm(client, tokens)
                    self._save(tokens)
            return tokens["access_token"]

    def request(self, method, path, **kwargs):
        if not path.startswith("/api/auth/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("invalid_cloud_api_path")
        token = self.access_token()
        with self.client_factory() as client:
            response = client.request(method, self.profile.server_url + path,
                                      headers={"Authorization": "Bearer " + token}, **kwargs)
            if response.status_code == 401:
                # A vendor's identity error does not mean the WorkSwarm session expired.
                # Only the broker's authentication boundary warrants refreshing it.
                error = self._json(response).get("error")
                if error == "authentication_required":
                    token = self.access_token(force=True)
                    response = client.request(method, self.profile.server_url + path,
                                              headers={"Authorization": "Bearer " + token}, **kwargs)
            data = self._json(response)
            if not response.is_success:
                raise AuthError(data.get("error") if isinstance(data.get("error"), str) else "cloud_request_failed",
                                response.status_code)
            return data

    def logout(self):
        with session_lock(self.root / "cloud-session-lock"):
            tokens = self._read()
            self.session_path.unlink(missing_ok=True)
        # Local platform logout never disconnects a provider or signs out other apps.
        return {"success": True, "authenticated": False, "local_session_deleted": True,
                "provider_connections_retained": True, "server_session_revoked": False}

    def open_connect_url(self, url):
        parsed, expected = urlsplit(url), urlsplit(self.profile.server_url)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc) or not parsed.path.startswith("/connect/") or parsed.query or parsed.fragment:
            raise AuthError("cloud_invalid_connect_url", 502)
        return bool(self.opener(url))
