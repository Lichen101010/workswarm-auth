"""Publisher runs locally on the server; secrets are hidden and never printed."""
import argparse
import getpass
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True, help="Generated config/providers.json")
    args = parser.parse_args()
    providers = json.loads(args.config.read_text(encoding="utf-8"))
    # Every provider the publisher rendered or added to providers.json, not just the defaults.
    for provider in providers:
        print(provider + ": leave both blank to retain its current configuration.")
        client_id = input("Client ID: ").strip()
        secret = getpass.getpass("Client Secret (hidden): ").strip()
        if bool(client_id) != bool(secret):
            raise SystemExit("Both client ID and secret are required; nothing written.")
        if client_id:
            providers[provider].update(client_id=client_id, client_secret=secret)
    os.umask(0o077)
    args.config.write_text(json.dumps(providers, indent=2) + "\n", encoding="utf-8")
    args.config.chmod(0o600)
    print("Publisher configuration saved; no secrets printed.")


if __name__ == "__main__":
    main()
