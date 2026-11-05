
import httpx
import pytest

from workswarm_auth.providers import google_error_code


@pytest.mark.parametrize("reason,expected", [
    ("SERVICE_DISABLED", "provider_api_not_enabled"),
    ("accessNotConfigured", "provider_api_not_enabled"),
    ("ACCESS_TOKEN_SCOPE_INSUFFICIENT", "scope_insufficient"),
    ("domainPolicy", "provider_domain_policy_denied"),
    ("unknown-private-value", "provider_access_denied"),
])
def test_google_error_reason_maps_to_safe_code(reason, expected):
    response = httpx.Response(403, json={"error": {"message": "private project details", "details": [{"reason": reason}]}})
    assert google_error_code(response) == expected
