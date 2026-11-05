"""Cloud-only boundaries and cancellation regressions after removing local mode."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from workswarm_auth.cloud import CloudClient, CloudProfile
from workswarm_auth.config import Settings
from workswarm_auth.errors import AuthError
from workswarm_auth.providers import Adapter
from test_universal_auth import authorize, begin, headers, owner_id, rig


def test_server_cannot_start_without_oidc_configuration(tmp_path):
    with pytest.raises(ValueError, match="server_requires_oidc_configuration"):
        Settings(tmp_path)


def test_server_does_not_accept_a_legacy_static_api_key(rig):
    client = rig[0]
    for value in ("", "Bearer " + "x" * 48):
        response = client.get("/api/auth/providers", headers={"Authorization": value})
        assert response.status_code == 401
    assert client.get("/api/auth/providers", headers=headers()).status_code == 200


def test_desktop_session_never_creates_provider_database(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSWARM_VAULT_KEY_FILE", str(tmp_path / "server-key-must-not-be-used"))
    client = CloudClient(tmp_path, CloudProfile("https://broker.example", "https://issuer.example"))
    client._save({"access_token": "platform-session-only", "subject": "alice"})
    assert client._read()["subject"] == "alice"
    assert list(tmp_path.rglob("*.sqlite3")) == []
    assert not (tmp_path / "providers.json").exists()
    assert b"platform-session-only" not in client.session_path.read_bytes()


def test_missing_platform_session_key_is_not_silently_regenerated(tmp_path):
    profile = CloudProfile("https://broker.example", "https://issuer.example")
    client = CloudClient(tmp_path, profile)
    client._save({"access_token": "platform-session", "subject": "alice"})
    (tmp_path / "cloud-session/vault.key").unlink()
    with pytest.raises(AuthError, match="cloud_session_key_missing"):
        CloudClient(tmp_path, profile)


@pytest.mark.parametrize("existing", [False, True])
def test_cancel_during_identity_lookup_prevents_late_commit(rig, monkeypatch, existing):
    client, app, vendor, _ = rig
    cid = "cancel-during-exchange"
    if existing:
        authorize(rig, cid=cid)
    owner, vault = owner_id(), app.state.vault
    before = vault.credentials(owner, cid)
    params, _ = begin(rig, cid=cid)
    attempt = vault.info(owner, cid)["flow"]
    original = Adapter.identity
    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()
        async def identity(adapter, token):
            result = await original(adapter, token)
            entered.set()
            await resume.wait()
            return result
        monkeypatch.setattr(Adapter, "identity", identity)
        task = asyncio.create_task(app.state.manager.callback("gmail", params["state"][0], "code", "", dict(client.cookies)))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            # This must finish while identity is still blocked, rather than
            # waiting on the callback's async lock until credentials commit.
            result = await asyncio.wait_for(app.state.manager.cancel(owner, cid, attempt), timeout=2)
            assert result["existing_credentials_retained"] is existing
        finally:
            resume.set()
        with pytest.raises(AuthError, match="connection_was_cancelled"):
            await task
    asyncio.run(scenario())
    assert vault.credentials(owner, cid) == before
    if not existing:
        assert vault.account(owner, cid) is None
    monkeypatch.setattr(Adapter, "identity", original)
    if not existing:
        vendor.subject = "retry-with-another-account"
    authorize(rig, cid=cid)
    assert vault.info(owner, cid)["authorization"] == "authorized"


def test_client_release_operates_without_any_server_modules(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from build_cloud_client import build
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(CloudProfile("https://broker.example", "https://issuer.example").binding))
    source = Path(__file__).resolve().parents[1]
    result = build(source, profile, tmp_path / "release")
    with zipfile.ZipFile(result["client_zip"]) as archive:
        modules = {Path(name).name for name in archive.namelist() if "/src/workswarm_auth/" in name}
        assert not modules.intersection({"app.py", "config.py", "vault.py", "providers.py", "manager.py", "security.py"})
        archive.extractall(tmp_path / "client")
    client_root = tmp_path / "client/workswarm-auth-cloud"
    code = "import sys;sys.path.insert(0,sys.argv.pop(1));from workswarm_auth.cli import main;main()"
    base = [sys.executable, "-I", "-c", code, str(client_root / "src")]
    home = tmp_path / "user-home"
    initialized = subprocess.run([*base, "cloud-init", "--home", str(home), "--profile", str(client_root / "publisher/cloud.json")],
                                 check=True, capture_output=True, text=True)
    assert json.loads(initialized.stdout)["provider_credentials_required"] is False
    configured = subprocess.run([*base, "cloud-config", "--home", str(home)], check=True, capture_output=True, text=True)
    assert "workswarm-auth-cloud" in json.loads(configured.stdout)["mcpServers"]
    assert list(home.rglob("*.sqlite3")) == []


def test_publisher_wheel_profile_initializes_a_fresh_home(tmp_path, monkeypatch):
    from argparse import Namespace
    from workswarm_auth import cloud_commands
    bundled = tmp_path / "publisher_profile.json"
    bundled.write_text(json.dumps({"server_url": "https://auth.example.com",
                                   "issuer": "https://login.example.com/realms/workswarm"}))
    monkeypatch.setattr(cloud_commands, "BUNDLED_PROFILE", bundled)
    home = tmp_path / "home"
    assert cloud_commands.run(Namespace(command="cloud-config", profile=None), home) == 0
    assert CloudProfile.load(home / "cloud.json").server_url == "https://auth.example.com"
    # An existing home keeps its own profile.
    (home / "cloud.json").write_text(json.dumps({"server_url": "https://other.example.com",
                                                 "issuer": "https://login.other.example.com/realms/workswarm"}))
    cloud_commands.run(Namespace(command="cloud-config", profile=None), home)
    assert CloudProfile.load(home / "cloud.json").server_url == "https://other.example.com"
