#!/usr/bin/env python3
"""A stand-in for Scalable's `sc` CLI in the tests: same envelope, exit codes and files.

Controlled through files in FAKE_SC_DIR: `confirmed` (the user confirmed the
device login), `mode` (no_grant | relogin), `data.json` (answers by command),
`calls` (every call is appended).
"""
import json
import os
import sys
import time
from pathlib import Path

ctl = Path(os.environ["FAKE_SC_DIR"])
cfg = Path(os.environ["XDG_CONFIG_HOME"]) / "scalable-cli"
args = sys.argv[1:]
with open(ctl / "calls", "a") as f:
    f.write(" ".join(args) + "\n")
mode = (ctl / "mode").read_text().strip() if (ctl / "mode").exists() else ""


def fail(code, message, exit_code=20):
    print(json.dumps({"ok": False, "command": ".".join(a for a in args if not a.startswith("-")),
                      "error": {"code": code, "message": message}}))
    sys.exit(exit_code)


if args[0] == "login":
    assert "--local-read-only" in args
    if mode == "no_grant":
        print("Error: OAuth error unauthorized_client: grant type not enabled for this client")
        sys.exit(20)
    print("Open this URL:\nhttps://secure.scalable.example/device?user_code=ABCD-EFGH\n")
    print("Verify the code ABCD-EFGH in your browser.\n", flush=True)
    for _ in range(600):
        if (ctl / "confirmed").exists():
            (cfg / "session.json").write_text(json.dumps({"session": {"refresh_token": "r1"}}))
            (cfg / "auth-signing-key.json").write_text('{"kty": "EC"}')
            print("Logged in.")
            sys.exit(0)
        time.sleep(0.05)
    print("Error: Device code login expired before completion")
    sys.exit(20)

assert args[-1] == "--json", args
if not (cfg / "session.json").exists():
    fail("no_session", "No active session. Run 'sc login'.")
if mode == "relogin":
    fail("refresh_relogin_required", "Token refresh requires a new login. Run 'sc login'.")
# Every call rotates the refresh token, like the real CLI.
session = json.loads((cfg / "session.json").read_text())
n = int(session["session"]["refresh_token"][1:]) + 1
(cfg / "session.json").write_text(json.dumps({"session": {"refresh_token": f"r{n}"}}))
data = json.loads((ctl / "data.json").read_text()) if (ctl / "data.json").exists() else {}
key = " ".join(a for a in args[:3] if not a.startswith("-"))
if key.startswith("broker transaction details"):
    key = "details " + args[args.index("--transaction-id") + 1]
elif key.startswith("broker transactions"):
    key = "transactions " + (args[args.index("--cursor") + 1] if "--cursor" in args else "first")
answer = data.get(key, {})
if args[0] == "broker":
    # Like sc: broker commands wrap their answer with the account and portfolio they used.
    answer = {"account_id": "acc-1", "portfolio_id": "pf-1",
              "resolution": {"account": "auto", "portfolio": "auto"}, "result": answer}
print(json.dumps({"ok": True, "command": key, "data": answer}))
