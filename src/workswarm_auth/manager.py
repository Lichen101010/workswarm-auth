import asyncio
import secrets
import time

from .config import SAFE_ID
from .providers import AuthError
from .security import check_browser

# Vendor-side failures that say nothing about the grant itself.
TRANSIENT_ERRORS = frozenset({"provider_network_error", "provider_rate_limited",
                              "provider_request_failed", "provider_invalid_response"})


class Manager:
    def __init__(self, settings, vault, registry):
        self.settings, self.vault, self.registry = settings, vault, registry
        self.locks = {}

    def lock(self, owner, cid):
        return self.locks.setdefault((owner, cid), asyncio.Lock())

    def info(self, owner, cid):
        result = self.vault.info(owner, cid)
        if result is None:
            raise AuthError("connection_not_found", 404)
        tokens = self.vault.credentials(owner, cid)
        result.pop("owner", None)
        result["pending_attempt_id"] = result.pop("flow", None)
        result["access_token_expired"] = bool(tokens and tokens.get("expires_at") is not None and tokens["expires_at"] <= time.time())
        result["refresh_available"] = bool(tokens and tokens.get("refresh_token"))
        result["credential_present"] = bool(tokens)
        result["identity_pending"] = bool(tokens and tokens.get("identity_unverified"))
        result["account"] = self.vault.account(owner, cid)
        return result

    async def begin(self, owner, provider, cid=None, label=""):
        adapter = self.registry.get(provider)
        callback = self.settings.callback(provider)
        adapter.check_configuration(callback)
        cid = cid or provider + "-" + secrets.token_hex(6)
        if not isinstance(cid, str) or not SAFE_ID.fullmatch(cid) or not isinstance(label, str) or len(label) > 120:
            raise AuthError("invalid_connection_id_or_label")
        async with self.lock(owner, cid):
            try:
                revision, flow = self.vault.begin(owner, cid, provider, label)
            except ValueError:
                raise AuthError("connection_provider_cannot_change", 409) from None
            ticket = self.vault.issue("auth_ticket", {"owner": owner, "cid": cid, "provider": provider, "revision": revision,
                                                      "flow": flow})
        return {"success": True, "connection_id": cid, "provider": provider, "revision": revision, "attempt_id": flow,
                "authorization": self.vault.info(owner, cid)["authorization"], "pending": True,
                "expires_in": 600, "connect_url": self.settings.origin + "/connect/" + ticket}

    async def cancel(self, owner, cid, attempt_id):
        # Do not wait on an in-flight token exchange: cancel its SQLite CAS now,
        # so a callback finishing later cannot commit credentials or an account pin.
        info = self.info(owner, cid)
        if (not isinstance(attempt_id, str) or not attempt_id or info["pending_attempt_id"] != attempt_id
                or not self.vault.fail_flow(owner, cid, info["revision"], attempt_id, "user_cancelled")):
            raise AuthError("authorization_attempt_not_pending", 409)
        return {"success": True, "connection_id": cid, "attempt_cancelled": True,
                "existing_credentials_retained": info["credential_present"]}

    def flow_active(self, flow):
        info = self.vault.info(flow["owner"], flow["cid"])
        return bool(info and info["revision"] == flow["revision"] and info["flow"] == flow["flow"])

    def open_link(self, ticket):
        """Consume a one-use connect link and return its still-current flow."""
        flow = self.vault.consume(ticket, "auth_ticket")
        if not flow:
            raise AuthError("connection_link_expired_or_used")
        if not self.flow_active(flow):
            raise AuthError("connection_link_cancelled")
        return flow

    def provider_redirect(self, flow):
        verifier = secrets.token_urlsafe(64)
        callback = self.settings.callback(flow["provider"])
        state = self.vault.issue("oauth_state", {**flow, "verifier": verifier, "redirect_uri": callback})
        return self.registry.get(flow["provider"]).authorization_url(state, verifier, callback)

    async def callback(self, provider, state, code, error, cookies):
        flow = self.vault.consume(state, "oauth_state")
        if not flow or flow["provider"] != provider:
            raise AuthError("invalid_expired_or_replayed_state")
        # A forwarded vendor URL completed in another browser must not touch this flow.
        check_browser(flow, cookies)
        owner, cid, revision, flow_id = flow["owner"], flow["cid"], flow["revision"], flow["flow"]
        async with self.lock(owner, cid):
            if not self.flow_active(flow):
                raise AuthError("connection_was_cancelled", 409)
            if error or not code:
                self.vault.fail_flow(owner, cid, revision, flow_id, "user_denied" if error else "missing_code")
                raise AuthError("user_denied" if error else "missing_code")
            try:
                adapter = self.registry.get(provider)
                tokens = await adapter.exchange(code, flow["verifier"], flow["redirect_uri"])
                identity = await adapter.identity(tokens["access_token"])
                # Credentials and the account pin commit together or not at all.
                if not self.vault.complete(owner, cid, revision, flow_id, tokens, identity):
                    raise AuthError("connection_was_cancelled", 409)
            except ValueError as exc:
                if str(exc) != "account_mismatch_use_new_connection":
                    raise
                # A grant for another account is rejected; the pinned account keeps its credentials.
                self.vault.fail_flow(owner, cid, revision, flow_id, "account_mismatch_use_new_connection")
                self.vault.audit(owner, cid, "identity", "account_mismatch")
                raise AuthError("account_mismatch_use_new_connection", 409) from None
            except AuthError as exc:
                self.vault.fail_flow(owner, cid, revision, flow_id, exc.code)
                self.vault.audit(owner, cid, "oauth_callback", exc.code)
                raise
        self.vault.audit(owner, cid, "oauth_callback", "success")
        return {"success": True, "connection_id": cid, "authorization": "authorized", "validation": "not_checked"}

    async def bind_identity(self, owner, cid, revision, adapter, token):
        """Check stored credentials still belong to the pinned account; a different account removes them."""
        identity = await adapter.identity(token)
        try:
            if not self.vault.bind(owner, cid, revision, identity):
                raise AuthError("connection_was_cancelled", 409)
        except ValueError:
            self.vault.disconnect(owner, cid, "account_mismatch_use_new_connection", expected_revision=revision)
            self.vault.audit(owner, cid, "identity", "account_mismatch")
            raise AuthError("account_mismatch_use_new_connection", 409) from None

    async def access_token(self, owner, cid, force=False):
        async with self.lock(owner, cid):
            info = self.info(owner, cid)
            tokens = self.vault.credentials(owner, cid)
            if not tokens or info["authorization"] != "authorized":
                raise AuthError("authorization_required", 401)
            revision, adapter = info["revision"], self.registry.get(info["provider"])
            expires = tokens.get("expires_at")
            if force or (expires is not None and expires <= time.time() + 60):
                try:
                    fresh = await adapter.refresh(tokens)
                except AuthError as exc:
                    if exc.code == "reauthorization_required":
                        self.vault.disconnect(owner, cid, exc.code, expected_revision=revision)
                    raise
                # Persist first (a rotated refresh token is single-use), marked unverified
                # until the account behind the new access token has been confirmed.
                tokens = {**fresh, "identity_unverified": True}
                if not self.vault.save(owner, cid, revision, tokens):
                    raise AuthError("connection_was_cancelled", 409)
            if tokens.get("identity_unverified"):
                # Retried on every use until it succeeds; a different account removes the grant.
                await self.bind_identity(owner, cid, revision, adapter, tokens["access_token"])
                tokens.pop("identity_unverified")
                if not self.vault.save(owner, cid, revision, tokens):
                    raise AuthError("connection_was_cancelled", 409)
            return tokens["access_token"], revision

    async def operation(self, owner, cid, operation, arguments):
        info = self.info(owner, cid)
        adapter = self.registry.get(info["provider"])
        if operation not in adapter.definition.operations:
            raise AuthError("operation_not_allowed", 403)
        token, revision = await self.access_token(owner, cid)
        if not self.vault.account(owner, cid):
            await self.bind_identity(owner, cid, revision, adapter, token)
        try:
            data = await adapter.operation(operation, token, arguments)
        except AuthError as exc:
            if exc.code != "reauthorization_required":
                raise
            token, revision = await self.access_token(owner, cid, force=True)
            try:
                data = await adapter.operation(operation, token, arguments)
            except AuthError as retry:
                if retry.code == "reauthorization_required":
                    self.vault.disconnect(owner, cid, retry.code, expected_revision=revision)
                raise
        current = self.vault.info(owner, cid)
        if not current or current["revision"] != revision or current["authorization"] != "authorized":
            raise AuthError("connection_was_cancelled", 409)
        return {"success": True, "connection_id": cid, "operation": operation, "data": data,
                "content_is_untrusted": True, "revision": revision}

    async def validate(self, owner, cid):
        info = self.info(owner, cid)
        probe = self.registry.get(info["provider"]).definition.probe
        try:
            token, revision = await self.access_token(owner, cid)
            await self.bind_identity(owner, cid, revision, self.registry.get(info["provider"]), token)
            result = await self.operation(owner, cid, probe, {"limit": 1})
        except AuthError as exc:
            # A network blip or rate limit keeps the last definitive validation result.
            if exc.code not in TRANSIENT_ERRORS:
                self.vault.fail_validation(owner, cid, info["revision"], exc.code)
            raise
        if not self.vault.verified(owner, cid, result["revision"]):
            raise AuthError("connection_was_cancelled", 409)
        # The probe data is deliberately discarded: validation reports are safe
        # to share and do not contain message contents, subjects, IDs or tokens.
        return {"success": True, "connection_id": cid, "provider": info["provider"],
                "authorization": "authorized", "validation": "passed", "probe": probe,
                "checked_at": self.info(owner, cid)["checked_at"],
                "coverage": "Only the named read-only API operation was verified."}

    async def disconnect(self, owner, cid):
        async with self.lock(owner, cid):
            info = self.info(owner, cid)
            tokens = self.vault.credentials(owner, cid)
            self.vault.disconnect(owner, cid)
            result = {"remote_revoked": True, "manual_revoke_url": None}
            if tokens:
                result = await self.registry.get(info["provider"]).revoke(tokens)
        return {"success": True, "connection_id": cid, "authorization": "disconnected",
                "credentials_deleted": True, **result}
