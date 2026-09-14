#!/usr/bin/env python3
"""Developer-only pairing/direct-HTTPS probe. Does not launch the agent or any job."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from trade_compass_agent.mobile.client import request_json


def write_private(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def local_request(port: int, method: str, path: str, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, f"/api/mobile/{path}",
                     json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        result = json.loads(response.read())
        if response.status >= 400:
            raise RuntimeError(f"{response.status}: {result.get('detail', 'Request failed')}")
        return result
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-port", type=int, default=19704)
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite")
    invite.add_argument("--endpoint", required=True, help="Computer's reachable HTTPS origin")
    invite.add_argument("--out", type=Path, required=True)
    commands.add_parser("devices")
    approve = commands.add_parser("approve")
    approve.add_argument("--device-id", required=True)
    approve.add_argument("--code", required=True, help="Compare with the code on the client first")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--device-id", required=True)
    pair = commands.add_parser("pair")
    pair.add_argument("--invitation", required=True, type=Path)
    pair.add_argument("--state", required=True, type=Path)
    pair.add_argument("--name", required=True)
    read = commands.add_parser("read")
    read.add_argument("--state", required=True, type=Path)
    read.add_argument("--resource", choices=["sessions", "notifications", "pairing/status", "info"],
                      default="sessions")
    read.add_argument("--session-id")
    read.add_argument("--before", type=int)
    args = parser.parse_args()
    if args.command == "invite":
        result = local_request(args.local_port, "POST", "pairing/invitations")
        write_private(args.out, {**result, "endpoint": args.endpoint})
        print(f"Invitation saved to {args.out}; transfer it only to the intended client.")
        return
    if args.command == "devices":
        result = local_request(args.local_port, "GET", "devices")
    elif args.command == "approve":
        result = local_request(args.local_port, "POST", f"devices/{quote(args.device_id, safe='')}/approve",
                               {"verification_code": args.code})
    elif args.command == "revoke":
        result = local_request(args.local_port, "DELETE", f"devices/{quote(args.device_id, safe='')}")
    elif args.command == "pair":
        invitation = json.loads(args.invitation.read_text())
        if invitation["protocol_version"] != 1:
            raise ValueError("Unsupported pairing protocol")
        state = {key: invitation[key] for key in ("endpoint", "certificate_sha256", "computer_id")}
        state["device_secret"] = secrets.token_urlsafe(32)
        # Save before sending: a lost claim response is recoverable with pairing/status.
        write_private(args.state, state)
        status, result = request_json(state["endpoint"], state["certificate_sha256"], "POST",
                                      "/mobile/v1/pairing/claim", body={
                                          "invitation": invitation["invitation"],
                                          "device_secret": state["device_secret"], "name": args.name,
                                      })
        if status != 202:
            raise RuntimeError(f"Pairing failed ({status}); retained state file: {args.state}")
    else:
        state = json.loads(args.state.read_text())
        path = f"/mobile/v1/{args.resource}"
        if args.session_id:
            path = f"/mobile/v1/sessions/{quote(args.session_id, safe='')}/messages"
            if args.before is not None:
                path += f"?before={args.before}"
        status, result = request_json(state["endpoint"], state["certificate_sha256"], "GET", path,
                                      secret=state["device_secret"])
        if status >= 400:
            raise RuntimeError(f"{status}: {result.get('detail', 'Request failed')}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
