"""Build allowlisted artifacts: client wheel and market connector.

Server config, env files, vaults, tests and live runtime directories are never
walked or copied. The only publisher input allowed is the public cloud profile.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile

from workswarm_auth import __version__
from workswarm_auth.cloud import CloudProfile


CLIENT_MODULES = ("__init__.py", "cli.py", "cloud.py", "cloud_commands.py", "cloud_mcp.py",
                  "session_store.py", "errors.py", "identifiers.py")


def build_wheel(source, public, path):
    """A pure-Python client wheel with the publisher profile inside; built directly, no build backend."""
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dist = f"workswarm_auth-{__version__}.dist-info"
    files = {f"workswarm_auth/{name}": (source / "src/workswarm_auth" / name).read_bytes() for name in CLIENT_MODULES}
    files["workswarm_auth/publisher_profile.json"] = public.encode()
    files[f"{dist}/METADATA"] = ("Metadata-Version: 2.1\nName: workswarm-auth\nVersion: " + __version__ + "\n"
        "Summary: WorkSwarm cloud authorization client (publisher build)\nRequires-Python: "
        + project["requires-python"] + "\n" + "".join(f"Requires-Dist: {dep}\n" for dep in project["dependencies"])).encode()
    files[f"{dist}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: workswarm-auth build_cloud_client\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{dist}/entry_points.txt"] = b"[console_scripts]\nworkswarm-auth = workswarm_auth.cli:main\n"
    record = "".join(f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
                     for name, data in files.items()) + f"{dist}/RECORD,,\n"
    files[f"{dist}/RECORD"] = record.encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


def launcher(origin):
    """``py -3 -c`` runs the bootstrap; base64 keeps the code intact through Windows argument quoting."""
    code = (Path(__file__).with_name("client_bootstrap.py").read_text(encoding="utf-8")
            .replace("__VERSION__", __version__).replace("__ORIGIN__", origin))
    return ["-3", "-c", "import base64;exec(base64.b64decode('" + base64.b64encode(code.encode()).decode() + "'))"]


def build(source, profile_path, output):
    profile = CloudProfile.load(profile_path)
    output.mkdir(parents=True, exist_ok=True)
    wheel = output / f"workswarm_auth-{__version__}-py3-none-any.whl"
    market_zip = output / f"workswarm-auth-cloud-market-{__version__}.zip"
    if wheel.exists() or market_zip.exists():
        raise ValueError("cloud_release_exists_choose_another_output")
    public = json.dumps(profile.binding, indent=2) + "\n"
    instructions = (
        "# WorkSwarm Cloud Authorization\n\n"
        "Requires Python 3.11+ with the py launcher (python.org installer default). No Google/GitHub/Microsoft/Slack "
        "client ID, secret or token is needed.\n\n"
        "Install this connector from the market and connect: on first start it downloads the client from "
        + profile.server_url + "/client/ into %LOCALAPPDATA%\\WorkSwarmAuth\\" + __version__ + " and installs it "
        "into its own virtual environment (needs pip access to PyPI or a mirror); later starts reuse it.\n\n"
        "Call auth_providers, then auth_connect(provider=...). On first use sign in to the publisher identity service "
        "(or with a linked Google/GitHub account if the publisher enabled it), then consent on the provider's "
        "official page in the same browser. Poll auth_status until authorization=authorized and "
        "validation=passed. Initial sign-in may need a 240s host tool timeout. If a tool later returns "
        "workswarm_login_required, call auth_login to sign in again; existing connections are kept.\n\n"
        "The cloud service and provider applications must already be deployed by the publisher; the publisher "
        "copies " + wheel.name + " into the broker's client/ directory.\n")
    build_wheel(source, public, wheel)
    summary = {"zh": "由发布者云端托管 OAuth，用户无需填写第三方凭证；首次连接自动安装客户端（需 Python 3.11+）。",
               "en": "Publisher-hosted OAuth with no end-user provider credentials. The client installs itself on first connect (Python 3.11+ required)."}
    manifest = {"id": "workswarm-auth-cloud", "name": "WorkSwarm Cloud Authorization", "version": __version__,
                "package_type": "mcp", "description": summary["en"],
                "display_name": {"zh": "第三方统一授权（云端托管）", "en": "Cloud Authorization"},
                # WorkSwarm's MCP package loader requires both localized fields.
                "display_description": summary,
                "integration": {"type": "stdio-mcp", "file": "mcp.json"}, "category": "办公与生产力"}
    mcp = {"mcpServers": {"workswarm-auth-cloud": {"command": "py", "type": "stdio", "args": launcher(profile.server_url)}}}
    with zipfile.ZipFile(market_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in (("manifest.json", manifest), ("mcp.json", mcp)):
            archive.writestr("workswarm-auth-cloud/" + name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("workswarm-auth-cloud/README.md", instructions)
    return {"client_wheel": str(wheel), "market_zip": str(market_zip),
            "published": False, "server_deployment_required": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parents[1], args.profile, args.output)))


if __name__ == "__main__":
    main()
