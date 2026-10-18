"""Start a single gateway worker behind a TLS reverse proxy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app import App
from .auth import Auth
from .store import Binding, GatewayError, Policy, Store
from .webhooks import Events


def build(config_path: Path):
    config = json.loads(config_path.read_text("utf-8"))
    if set(config) != {"base_url", "state_dir", "key_file", "owner_principal", "owner_secret_hash",
                       "service_tokens", "redirect_uris", "bindings", "policy"}:
        raise GatewayError("Unexpected gateway configuration fields.")
    state_dir, key_file = Path(config["state_dir"]), Path(config["key_file"])
    if not state_dir.is_absolute() or not state_dir.is_dir() or state_dir.is_symlink() or not key_file.is_absolute() or key_file.is_symlink():
        raise GatewayError("An existing private state directory and key file are required.")
    policy = Policy(**config["policy"])
    bindings = tuple(Binding(**b) for b in config["bindings"])
    if policy.enabled and not bindings:
        raise GatewayError("Live collection requires an explicit room binding.")
    store = Store(state_dir / "gateway.sqlite3", bindings, policy)
    auth = Auth(store, config["base_url"], config["owner_principal"], config["owner_secret_hash"],
                config["service_tokens"], tuple(config["redirect_uris"]))
    events = Events(store, key_file.read_bytes().strip(), has_grant=auth.has_event_grant)
    return App(store, events, auth)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a nonprivileged port.")
    import uvicorn
    uvicorn.run(build(args.config.resolve()), host="127.0.0.1", port=args.port, workers=1,
                access_log=False, server_header=False, proxy_headers=False,
                timeout_keep_alive=5, limit_concurrency=16)


if __name__ == "__main__":
    main()
