import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .identifiers import SAFE_ID

DEFAULT_ROOT = Path.home() / ".workswarm-auth-server"


@dataclass
class ProviderSettings:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    tenant: str = "common"
    token_kind: str = "bot"
    pkce: bool = True


@dataclass
class Settings:
    root: Path
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    port: int = 8766
    remote: dict = field(default_factory=dict)

    def __post_init__(self):
        if not 1024 <= self.port <= 65535:
            raise ValueError("invalid_port")
        if not self.remote:
            raise ValueError("server_requires_oidc_configuration")
        # Connect links are opened in a browser that carries no MCP token; the owner
        # signs in to the same issuer there so a link cannot bind someone else's account.
        login = self.remote.get("owner_login")
        if (not isinstance(login, dict) or not isinstance(login.get("client_id"), str) or not login["client_id"]
                or not isinstance(login.get("client_secret", ""), str)):
            raise ValueError("remote_requires_owner_login_client")
        urls = [self.remote.get(key, "") for key in ("issuer", "jwks_uri", "public_origin")]
        urls += [login.get(key, "") for key in ("authorization_endpoint", "token_endpoint")]
        for value in urls:
            parsed = urlsplit(value) if isinstance(value, str) else urlsplit("")
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError("remote_urls_require_https")
        if urlsplit(self.remote["public_origin"]).path or self.remote["public_origin"].endswith("/"):
            raise ValueError("remote_public_origin_must_have_no_path_or_trailing_slash")
        principals = self.remote.get("principals", {})
        enrollment = self.remote.get("enrollment")
        if not isinstance(principals, dict) or (not principals and enrollment is None):
            raise ValueError("remote_requires_explicit_principal_policies")
        policies = list(principals.items())
        if enrollment is not None:
            if (not isinstance(enrollment, dict) or set(enrollment) != {"group", "providers", "operations"}
                    or not isinstance(enrollment["group"], str) or not enrollment["group"]
                    or len(enrollment["group"]) > 200):
                raise ValueError("remote_enrollment_requires_group_and_explicit_policy")
            policies.append(("group-policy", enrollment))
        for subject, policy in policies:
            if not isinstance(subject, str) or not subject or not isinstance(policy, dict):
                raise ValueError("invalid_principal_policy")
            for key in ("providers", "operations"):
                entries = policy.get(key)
                if not isinstance(entries, list) or not all(isinstance(item, str) and SAFE_ID.fullmatch(item) for item in entries):
                    raise ValueError("policy_requires_explicit_provider_and_operation_lists")
        for provider_id, config in self.providers.items():
            if not SAFE_ID.fullmatch(provider_id) or not SAFE_ID.fullmatch(config.tenant):
                raise ValueError("invalid_provider_or_tenant")
            if config.token_kind not in {"bot", "user"}:
                raise ValueError("invalid_slack_token_kind")
            if not isinstance(config.pkce, bool):
                raise ValueError("pkce_must_be_boolean")
            if config.redirect_uri:
                parsed = urlsplit(config.redirect_uri)
                try:
                    port = parsed.port
                except ValueError:
                    raise ValueError("invalid_redirect_uri") from None
                expected_path = f"/oauth/providers/{provider_id}/callback"
                if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                        or parsed.query or parsed.fragment or parsed.path != expected_path):
                    raise ValueError("redirect_uri_must_target_provider_callback_over_https")

    @property
    def origin(self):
        return self.remote["public_origin"]

    def callback(self, provider_id):
        config = self.providers.get(provider_id, ProviderSettings())
        return config.redirect_uri or f"{self.origin}/oauth/providers/{provider_id}/callback"


def load_settings(root=None):
    root = Path(root or os.environ.get("WORKSWARM_AUTH_HOME", DEFAULT_ROOT)).expanduser()
    values = os.environ
    providers_path = Path(values.get("WORKSWARM_PROVIDER_CONFIG_FILE") or root / "providers.json")
    raw = json.loads(providers_path.read_text(encoding="utf-8-sig")) if providers_path.exists() else {}
    providers = {key: ProviderSettings(**value) for key, value in raw.items()}
    remote_path = Path(values.get("WORKSWARM_REMOTE_CONFIG_FILE") or root / "remote.json")
    remote = json.loads(remote_path.read_text(encoding="utf-8-sig")) if remote_path.exists() else {}
    return Settings(root, providers, int(values.get("AUTH_PORT", "8766")), remote)

