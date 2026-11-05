"""Audience-bound JWT verification and explicit per-principal authorization."""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import jwt
import httpx

from .config import SAFE_ID
from .providers import AuthError

SCOPES = ("auth:read", "auth:connect", "auth:use", "auth:disconnect")

# Every step after /connect (owner sign-in, vendor callback) must come from the browser that
# opened the link; otherwise a forwarded next-hop URL lets another person finish the flow.
BINDING_COOKIE_PREFIX = "ws_auth_"
BINDING_COOKIE_PATH = "/oauth/"


def bind_browser(flow):
    """Return (flow carrying the binding digest, cookie name, cookie secret)."""
    secret = secrets.token_urlsafe(32)
    return ({**flow, "binding": hashlib.sha256(secret.encode()).hexdigest()},
            BINDING_COOKIE_PREFIX + flow["flow"], secret)


def check_browser(flow, cookies):
    presented = cookies.get(BINDING_COOKIE_PREFIX + str(flow.get("flow")), "")
    expected = flow.get("binding")
    if not presented or not isinstance(expected, str) or not secrets.compare_digest(
            hashlib.sha256(presented.encode()).hexdigest(), expected):
        raise AuthError("browser_binding_mismatch", 403)


@dataclass(frozen=True)
class AccessToken:
    """A verified platform access token; the audience is the broker's resource URL."""
    subject: str
    scopes: tuple[str, ...]
    claims: dict


@dataclass(frozen=True)
class Principal:
    owner: str
    scopes: frozenset[str] = frozenset(SCOPES)
    providers: tuple[str, ...] | None = None
    operations: tuple[str, ...] | None = None


class Verifier:
    """Accept only JWT access tokens from the configured issuer and JWKS URL.

    This is a resource server. Login, PKCE, client registration and token issuance
    belong to the external authorization server, not this component.
    """
    def __init__(self, config, client):
        self.config, self.client = config, client
        self.keys, self.expires = {}, 0
        self.lock = asyncio.Lock()

    async def decode(self, token, audience):
        """Verify signature, issuer, audience and expiry; return claims for a registered subject or None."""
        if not isinstance(token, str) or len(token) > 16384:
            return None
        try:
            header = jwt.get_unverified_header(token)
            kid, alg = header.get("kid"), header.get("alg")
            if not isinstance(kid, str) or len(kid) > 200 or alg not in {"RS256", "ES256"}:
                return None
            async with self.lock:
                # Unknown kids cannot cause unlimited attacker-driven JWKS fetches.
                if time.time() >= self.expires:
                    response = await self.client.get(self.config["jwks_uri"])
                    response.raise_for_status()
                    raw = response.json()
                    if not isinstance(raw.get("keys"), list) or len(raw["keys"]) > 100:
                        return None
                    self.keys = {item["kid"]: item for item in raw["keys"] if isinstance(item, dict) and isinstance(item.get("kid"), str)}
                    self.expires = time.time() + 60
            material = self.keys.get(kid)
            if not material or material.get("use", "sig") != "sig" or material.get("alg", alg) != alg:
                return None
            key = jwt.PyJWK.from_dict(material, algorithm=alg).key
            claims = jwt.decode(token, key, algorithms=[alg], audience=audience,
                                issuer=self.config["issuer"], options={"require": ["exp", "iss", "sub", "aud"]})
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject or len(subject) > 512 or self.policy(claims) is None:
                return None
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, httpx.HTTPError):
            return None

    async def verify_token(self, token):
        claims = await self.decode(token, self.config["public_origin"] + "/mcp")
        if claims is None or not isinstance(claims.get("scope", ""), str):
            return None
        return AccessToken(subject=claims["sub"], scopes=tuple(claims.get("scope", "").split()), claims=claims)

    def owner(self, subject):
        digest = hashlib.sha256(json.dumps([self.config["issuer"], subject], separators=(",", ":")).encode()).hexdigest()
        return "oidc_" + digest[:48]

    def principal(self, token):
        policy = self.policy(token.claims)
        if policy is None:
            raise AuthError("authentication_required", 401)
        return Principal(self.owner(token.subject), frozenset(token.scopes), tuple(policy["providers"]), tuple(policy["operations"]))

    def policy(self, claims):
        """A publisher may admit members of one explicitly named IdP group.

        No client-supplied owner/group is accepted. The signed access AND ID tokens
        must carry membership, including at the browser owner-login boundary.
        Static subject policies remain compatible with earlier deployments.
        """
        policy = self.config.get("principals", {}).get(claims.get("sub"))
        if policy is not None:
            return policy
        enrollment = self.config.get("enrollment")
        groups = claims.get("groups")
        if enrollment and isinstance(groups, list) and all(isinstance(item, str) for item in groups):
            if enrollment["group"] in groups:
                return enrollment
        return None


class OwnerLogin:
    """Remote connect links: the browser that opens one must sign in as the link's owner.

    Without this, a user could forward their own link and have someone else's third-party
    account stored under their connection.
    """
    CALLBACK = "/oauth/owner/callback"

    def __init__(self, config, verifier, vault):
        self.config, self.verifier, self.vault = config, verifier, vault
        self.login = config["owner_login"]

    @property
    def redirect_uri(self):
        return self.config["public_origin"] + self.CALLBACK

    def start(self, flow):
        verifier, nonce = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        state = self.vault.issue("owner_login", {"flow": flow, "verifier": verifier, "nonce": nonce})
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return self.login["authorization_endpoint"] + "?" + urlencode({
            "client_id": self.login["client_id"], "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": "openid", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    async def finish(self, state, code, cookies):
        login = self.vault.consume(state, "owner_login")
        if not login or not code:
            raise AuthError("invalid_expired_or_replayed_state")
        # Before redeeming the code: a forwarded sign-in callback must not seed another browser.
        check_browser(login["flow"], cookies)
        data = {"grant_type": "authorization_code", "client_id": self.login["client_id"], "code": code,
                "code_verifier": login["verifier"], "redirect_uri": self.redirect_uri}
        if self.login.get("client_secret"):
            data["client_secret"] = self.login["client_secret"]
        try:
            response = await self.verifier.client.post(self.login["token_endpoint"], data=data, headers={"Accept": "application/json"})
            id_token = response.json().get("id_token") if response.is_success else None
        except (httpx.HTTPError, ValueError, AttributeError):
            raise AuthError("owner_login_failed", 502) from None
        claims = await self.verifier.decode(id_token, self.login["client_id"])
        if claims is None or claims.get("nonce") != login["nonce"]:
            raise AuthError("owner_login_failed", 401)
        if self.verifier.owner(claims["sub"]) != login["flow"]["owner"]:
            raise AuthError("connect_link_belongs_to_another_user", 403)
        return login["flow"]


class Gateway:
    """One policy boundary shared by HTTP and remote MCP transports."""
    def __init__(self, manager, principal):
        self.manager, self.principal = manager, principal

    async def call(self, action, cid="", provider="", operation="", arguments=None, label="", attempt_id=""):
        principal, manager = self.principal, self.manager
        scope = {"providers": "auth:read", "status": "auth:read", "audit": "auth:read", "connect": "auth:connect",
                 "validate": "auth:use", "call": "auth:use", "disconnect": "auth:disconnect", "cancel": "auth:connect"}[action]
        audit_cid = cid if isinstance(cid, str) and SAFE_ID.fullmatch(cid) else None
        try:
            if scope not in principal.scopes:
                raise AuthError("insufficient_scope", 403)
            if cid and (not isinstance(cid, str) or not SAFE_ID.fullmatch(cid)):
                raise AuthError("invalid_connection_id")
            if action not in {"connect", "providers", "audit"} and cid:
                provider = manager.info(principal.owner, cid)["provider"]
            if provider and principal.providers is not None and provider not in principal.providers:
                raise AuthError("provider_denied_by_policy", 403)
            if action == "validate":
                operation = manager.registry.get(provider).definition.probe
            if operation and principal.operations is not None and operation not in principal.operations:
                raise AuthError("operation_denied_by_policy", 403)
            if action == "providers":
                items = manager.registry.describe()
                if principal.providers is not None:
                    items = [item for item in items if item["id"] in principal.providers]
                    for item in items:
                        item["operations"] = [op for op in item["operations"] if op in principal.operations]
                result = {"success": True, "providers": items}
            elif action == "connect":
                result = await manager.begin(principal.owner, provider, cid or None, label)
            elif action == "status":
                if cid:
                    result = {"success": True, **manager.info(principal.owner, cid)}
                else:
                    items = [manager.info(principal.owner, row["connection_id"]) for row in manager.vault.list(principal.owner)]
                    result = {"success": True, "connections": [row for row in items if principal.providers is None or row["provider"] in principal.providers]}
            elif action == "validate":
                result = await manager.validate(principal.owner, cid)
            elif action == "call":
                result = await manager.operation(principal.owner, cid, operation, arguments or {})
                result.pop("revision", None)
            elif action == "disconnect":
                result = await manager.disconnect(principal.owner, cid)
            elif action == "cancel":
                result = await manager.cancel(principal.owner, cid, attempt_id)
            else:
                result = {"success": True, "events": manager.vault.events(principal.owner)}
            if action in {"connect", "validate", "call", "disconnect", "cancel"}:
                manager.vault.audit(principal.owner, audit_cid or result.get("connection_id"), action, "success")
            return result
        except AuthError as exc:
            manager.vault.audit(principal.owner, audit_cid, action, exc.code)
            raise
