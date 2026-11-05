"""Built-in adapters. Endpoints and read-only operations are code-owned, not LLM inputs."""
import base64
import hashlib
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx


from .errors import AuthError


def google_error_code(response):
    """Map a Google API 403 to a fixed code. Google's messages may contain project or account data."""
    try:
        error = response.json().get("error", {})
        reasons = {item.get("reason") for section in ("errors", "details")
                   for item in error.get(section, []) if isinstance(item, dict)} if isinstance(error, dict) else set()
    except (ValueError, AttributeError, TypeError):
        reasons = set()
    if reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
        return "provider_api_not_enabled"
    if reasons & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}:
        return "scope_insufficient"
    if "domainPolicy" in reasons:
        return "provider_domain_policy_denied"
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        return "provider_rate_limited"
    if "dailyLimitExceeded" in reasons:
        return "provider_quota_exceeded"
    return "provider_access_denied"


@dataclass(frozen=True)
class Definition:
    id: str
    family: str
    scopes: tuple[str, ...]
    probe: str
    operations: tuple[str, ...]


DEFINITIONS = {
    "gmail": Definition("gmail", "google", ("openid", "email", "https://www.googleapis.com/auth/gmail.readonly",), "gmail_search", ("gmail_search", "gmail_read")),
    "google-drive": Definition("google-drive", "google", ("openid", "email", "https://www.googleapis.com/auth/drive.metadata.readonly",), "drive_list", ("drive_list",)),
    "google-calendar": Definition("google-calendar", "google", ("openid", "email", "https://www.googleapis.com/auth/calendar.calendarlist.readonly",), "calendar_list", ("calendar_list",)),
    "github": Definition("github", "github", ("read:user",), "github_profile", ("github_profile", "github_repos")),
    "microsoft": Definition("microsoft", "microsoft", ("offline_access", "User.Read", "Mail.Read"), "outlook_list", ("microsoft_profile", "outlook_list")),
    "slack": Definition("slack", "slack", ("team:read",), "slack_identity", ("slack_identity",)),
}


class Adapter:
    """Subclass and register in Registry to add another vendor; use official APIs."""
    def __init__(self, definition, config, client):
        self.definition, self.config, self.client = definition, config, client

    @property
    def family(self):
        return self.definition.family

    @property
    def token_url(self):
        return {
            "google": "https://oauth2.googleapis.com/token",
            "github": "https://github.com/login/oauth/access_token",
            "microsoft": f"https://login.microsoftonline.com/{self.config.tenant}/oauth2/v2.0/token",
            "slack": "https://slack.com/api/oauth.v2.access",
        }[self.family]

    def check_configuration(self, redirect_uri):
        if not self.config.client_id:
            raise AuthError("provider_not_configured")
        if (self.family in {"google", "github"} or (self.family == "slack" and not self.config.pkce)) and not self.config.client_secret:
            raise AuthError("provider_client_secret_required")
        if self.family == "slack" and not redirect_uri.startswith("https://"):
            raise AuthError("slack_requires_https_callback")

    def authorization_url(self, state, verifier, redirect_uri):
        self.check_configuration(redirect_uri)
        endpoint = {
            "google": "https://accounts.google.com/o/oauth2/v2/auth",
            "github": "https://github.com/login/oauth/authorize",
            "microsoft": f"https://login.microsoftonline.com/{self.config.tenant}/oauth2/v2.0/authorize",
            "slack": "https://slack.com/oauth/v2/authorize",
        }[self.family]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": self.config.client_id, "redirect_uri": redirect_uri,
                  "response_type": "code", "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        if self.family == "slack" and not self.config.pkce:
            params.pop("code_challenge")
            params.pop("code_challenge_method")
        scopes = self.definition.scopes
        if self.family == "slack":
            params["user_scope" if self.config.token_kind == "user" else "scope"] = ",".join(scopes)
        else:
            params["scope"] = " ".join(scopes)
        if self.family == "google":
            params.update(access_type="offline", prompt="consent select_account")
        elif self.family == "microsoft":
            params["prompt"] = "select_account"
        return endpoint + "?" + urlencode(params)

    async def request_json(self, method, url, **kwargs):
        try:
            response = await self.client.request(method, url, **kwargs)
            payload = response.json() if response.content else {}
        except httpx.HTTPError:
            raise AuthError("provider_network_error", 502) from None
        except ValueError:
            raise AuthError("provider_invalid_response", 502) from None
        if not isinstance(payload, dict):
            raise AuthError("provider_invalid_response", 502)
        error = payload.get("error")
        if error == "invalid_grant" or error in ("invalid_auth", "token_revoked", "token_expired"):
            raise AuthError("reauthorization_required", 401)
        if response.status_code == 401:
            raise AuthError("reauthorization_required", 401)
        if response.status_code == 403:
            code = google_error_code(response) if self.family == "google" else "provider_access_denied"
            raise AuthError(code, 403)
        if response.status_code == 429:
            raise AuthError("provider_rate_limited", 429)
        if response.is_error or error or payload.get("ok") is False:
            if error == "missing_scope":
                raise AuthError("scope_insufficient", 403)
            raise AuthError("provider_request_failed", 502)
        return payload, response

    def normalize(self, payload, previous=None, returned_scope=None):
        if self.family == "slack" and self.config.token_kind == "user":
            # Initial installation nests user tokens; refresh returns a flat user token.
            payload = payload.get("authed_user", payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
            raise AuthError("invalid_token_response", 502)
        token_type = payload.get("token_type", "bearer")
        if not isinstance(token_type, str):
            raise AuthError("invalid_token_response", 502)
        token_type = token_type.lower()
        allowed = {"bearer"} if self.family != "slack" else {"bearer", "bot", "user"}
        if token_type not in allowed:
            raise AuthError("unsupported_token_type", 502)
        scope = payload.get("scope", returned_scope)
        if scope is None and previous:
            scope = previous.get("scope", "")
        if scope is None:
            # OAuth permits omission when the grant exactly matches the request.
            scope = " ".join(self.definition.scopes)
        if not isinstance(scope, str):
            raise AuthError("invalid_token_response", 502)
        granted = set(scope.replace(",", " ").split())
        if self.family == "github" and "user" in granted:
            granted.add("read:user")
        required = set(self.definition.scopes) - {"offline_access", "openid", "profile", "email"}
        if not required.issubset(granted):
            raise AuthError("scope_insufficient", 403)
        result = {"access_token": payload["access_token"], "scope": " ".join(sorted(granted)),
                  "refresh_token": payload.get("refresh_token") or (previous or {}).get("refresh_token"),
                  "expires_at": None}
        expiry = payload.get("expires_in")
        if expiry is not None:
            try:
                duration = float(expiry)
            except (ValueError, TypeError):
                raise AuthError("invalid_token_expiry", 502) from None
            if not math.isfinite(duration) or duration <= 0:
                raise AuthError("invalid_token_expiry", 502)
            result["expires_at"] = time.time() + duration
        elif self.family in {"google", "microsoft"}:
            result["expires_at"] = time.time() + 3600
        return result

    async def identity(self, token):
        """Read an immutable service identity using the issued credential."""
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        urls = {"google": "https://openidconnect.googleapis.com/v1/userinfo",
                "github": "https://api.github.com/user",
                "microsoft": "https://graph.microsoft.com/v1.0/me",
                "slack": "https://slack.com/api/auth.test"}
        try:
            payload, _ = await self.request_json("GET", urls[self.family], headers=headers)
        except AuthError as exc:
            # A rejected identity lookup means the grant lacks identity scopes (e.g. a
            # pre-openid Google token), not that the grant was revoked.
            if exc.code in {"reauthorization_required", "provider_access_denied", "scope_insufficient"}:
                raise AuthError("provider_identity_missing_reconnect_required", 401) from None
            raise
        if self.family == "google":
            subject = payload.get("sub")
            label = payload.get("email") or "Google account"
        elif self.family == "github":
            subject = str(payload["id"]) if isinstance(payload.get("id"), int) and not isinstance(payload["id"], bool) else None
            label = payload.get("login") or "GitHub account"
        elif self.family == "microsoft":
            subject = payload.get("id")
            label = payload.get("userPrincipalName") or payload.get("displayName") or "Microsoft account"
        else:
            team, user = payload.get("team_id"), payload.get("user_id")
            subject = team + ":" + user if isinstance(team, str) and team and isinstance(user, str) and user else None
            label = payload.get("user") or "Slack account"
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise AuthError("provider_identity_missing_reconnect_required", 401)
        return {"family": self.family, "subject": subject, "label": str(label)[:120]}

    async def exchange(self, code, verifier, redirect_uri):
        data = {"grant_type": "authorization_code", "client_id": self.config.client_id,
                "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri}
        if self.family == "slack" and not self.config.pkce:
            data.pop("code_verifier")
        if self.config.client_secret and not (self.family == "slack" and self.config.pkce):
            data["client_secret"] = self.config.client_secret
        payload, response = await self.request_json("POST", self.token_url, data=data, headers={"Accept": "application/json"})
        return self.normalize(payload, returned_scope=response.headers.get("X-OAuth-Scopes"))

    async def refresh(self, tokens):
        if not tokens.get("refresh_token"):
            raise AuthError("reauthorization_required", 401)
        data = {"grant_type": "refresh_token", "client_id": self.config.client_id,
                "refresh_token": tokens["refresh_token"]}
        if self.config.client_secret and not (self.family == "slack" and self.config.pkce):
            data["client_secret"] = self.config.client_secret
        if self.family == "microsoft":
            data["scope"] = " ".join(self.definition.scopes)
        payload, response = await self.request_json("POST", self.token_url, data=data, headers={"Accept": "application/json"})
        return self.normalize(payload, previous=tokens, returned_scope=response.headers.get("X-OAuth-Scopes"))

    async def operation(self, operation, token, arguments):
        if operation not in self.definition.operations:
            raise AuthError("operation_not_allowed", 403)
        if set(arguments) - {"query", "limit", "page_token", "message_id"}:
            raise AuthError("unsupported_operation_arguments")
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise AuthError("invalid_limit")
        query, page = arguments.get("query", ""), arguments.get("page_token", "")
        if not isinstance(query, str) or len(query) > 2000 or not isinstance(page, str) or len(page) > 4096:
            raise AuthError("invalid_query_or_page_token")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        params = {}
        if operation == "gmail_search":
            url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            params = {"q": query, "maxResults": limit}
            if page: params["pageToken"] = page
        elif operation == "gmail_read":
            mid = arguments.get("message_id", "")
            if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", mid):
                raise AuthError("invalid_message_id")
            url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/" + quote(mid, safe="")
            params = {"format": "full"}
        elif operation == "drive_list":
            url = "https://www.googleapis.com/drive/v3/files"
            params = {"pageSize": limit, "fields": "files(id,name,mimeType),nextPageToken"}
            if query: params["q"] = query
            if page: params["pageToken"] = page
        elif operation == "calendar_list":
            url = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
            params = {"maxResults": limit}
            if page: params["pageToken"] = page
        elif operation == "github_profile":
            url = "https://api.github.com/user"
        elif operation == "github_repos":
            url = "https://api.github.com/user/repos"
            params = {"per_page": limit, "type": "owner"}
        elif operation == "microsoft_profile":
            url = "https://graph.microsoft.com/v1.0/me"
            params = {"$select": "id,displayName,userPrincipalName"}
        elif operation == "outlook_list":
            url = "https://graph.microsoft.com/v1.0/me/messages"
            params = {"$top": limit, "$select": "id,subject,receivedDateTime"}
        else:
            url = "https://slack.com/api/auth.test"
        # GitHub's repository endpoint returns a top-level array.
        if operation == "github_repos":
            try:
                response = await self.client.get(url, params=params, headers=headers)
                if response.status_code == 401: raise AuthError("reauthorization_required", 401)
                if response.status_code == 403: raise AuthError("provider_access_denied", 403)
                if response.status_code == 429: raise AuthError("provider_rate_limited", 429)
                payload = response.json()
                if response.is_error or not isinstance(payload, list): raise AuthError("provider_request_failed", 502)
                return {"repositories": [{"id": item.get("id"), "full_name": item.get("full_name"), "private": item.get("private")} for item in payload]}
            except (httpx.HTTPError, ValueError):
                raise AuthError("provider_network_error", 502) from None
        payload, _ = await self.request_json("GET", url, params=params, headers=headers)
        if operation == "gmail_read":
            text = []
            def extract(part):
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    value = part["body"]["data"]
                    try: text.append(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", errors="replace"))
                    except ValueError: pass
                for child in part.get("parts", []): extract(child)
            part = payload.get("payload", {})
            extract(part)
            body = "\n".join(text)
            return {"id": payload.get("id"), "snippet": payload.get("snippet", ""),
                    "headers": {h["name"].lower(): h["value"] for h in part.get("headers", []) if h.get("name", "").lower() in {"subject", "from", "to", "date"}},
                    "text": body[:100000], "text_truncated": len(body) > 100000, "content_is_untrusted": True}
        if operation == "slack_identity":
            return {k: payload[k] for k in ("user_id", "team_id", "bot_id") if k in payload}
        if operation == "github_profile":
            return {k: payload[k] for k in ("login", "id", "name") if k in payload}
        if operation == "calendar_list":
            return {"items": [{k: item[k] for k in ("id", "summary", "accessRole") if k in item} for item in payload.get("items", [])], "nextPageToken": payload.get("nextPageToken")}
        if operation == "outlook_list":
            return {"messages": payload.get("value", [])}  # Never follow arbitrary @odata.nextLink.
        return payload

    async def revoke(self, tokens):
        if self.family == "microsoft":
            return {"remote_revoked": False, "manual_revoke_url": "https://myapplications.microsoft.com/"}
        try:
            if self.family == "google":
                response = await self.client.post("https://oauth2.googleapis.com/revoke", data={"token": tokens.get("refresh_token") or tokens["access_token"]})
            elif self.family == "github":
                # Delete this token, not every grant for the user's application.
                response = await self.client.request("DELETE", f"https://api.github.com/applications/{quote(self.config.client_id, safe='')}/token",
                    auth=(self.config.client_id, self.config.client_secret),
                    json={"access_token": tokens["access_token"]}, headers={"Accept": "application/vnd.github+json"})
            else:
                response = await self.client.post("https://slack.com/api/auth.revoke", headers={"Authorization": "Bearer " + tokens["access_token"]})
            ok = response.is_success
            if self.family == "slack":
                ok = ok and response.json().get("ok") is True
                # With token rotation auth.revoke only revokes the access token;
                # it does not remove the entire Slack installation.
                if tokens.get("refresh_token"):
                    return {"remote_revoked": False, "access_token_revoked": ok,
                            "manual_revoke_url": "https://slack.com/apps/manage"}
            return {"remote_revoked": ok, "manual_revoke_url": None if ok else self.manual_revoke_url}
        except (httpx.HTTPError, ValueError):
            return {"remote_revoked": False, "manual_revoke_url": self.manual_revoke_url}

    @property
    def manual_revoke_url(self):
        return {"google": "https://myaccount.google.com/connections", "github": "https://github.com/settings/applications",
                "slack": "https://slack.com/apps/manage", "microsoft": "https://myapplications.microsoft.com/"}[self.family]


class Registry:
    """Code-level extension point; custom adapters receive the same vault/manager lifecycle."""
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.definitions = dict(DEFINITIONS)
        self.adapter_classes = {key: Adapter for key in self.definitions}

    def register(self, definition, adapter_class):
        if definition.id in self.definitions:
            raise ValueError("provider_already_registered")
        self.definitions[definition.id] = definition
        self.adapter_classes[definition.id] = adapter_class

    def get(self, provider_id):
        from .config import ProviderSettings
        if provider_id not in self.definitions:
            raise AuthError("unknown_provider", 404)
        config = self.settings.providers.get(provider_id, ProviderSettings())
        return self.adapter_classes[provider_id](self.definitions[provider_id], config, self.client)

    def describe(self):
        result = []
        for pid, definition in self.definitions.items():
            adapter = self.get(pid)
            error = None
            try: adapter.check_configuration(self.settings.callback(pid))
            except AuthError as exc: error = exc.code
            result.append({"id": pid, "configured": error is None, "configuration_error": error,
                           "scopes": list(definition.scopes), "probe": definition.probe,
                           "operations": list(definition.operations)})
        return result

