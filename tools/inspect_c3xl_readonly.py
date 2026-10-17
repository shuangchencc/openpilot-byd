#!/usr/bin/env python3
"""Explicit read-only SSH inspection; no connection unless --connect is provided."""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "hardware/step1/collectors"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("system", "details", "messages", "recovery"))
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--connection", type=Path, default=ROOT / "local/c3xl-connection.json")
    args = parser.parse_args()
    source = (COLLECTORS / (args.phase + ".py")).read_bytes()
    if not args.connect:
        print(json.dumps({"mode": "preview_no_connection", "phase": args.phase,
                          "source_sha256": hashlib.sha256(source).hexdigest(),
                          "note": "Requires explicit --connect and existing SSH identity/known_hosts; no automatic hardware control."}))
        return 0
    c = json.loads(args.connection.read_text())
    ipaddress.ip_address(c["host"])
    if c["user"] != "comma" or type(c["port"]) is not int or not 1 <= c["port"] <= 65535:
        raise ValueError("Unexpected SSH endpoint")
    identity = Path(c["identity_file"])
    if not identity.is_file():
        raise ValueError("Existing SSH identity path required")
    python = "/usr/bin/python3"
    command = ["ssh", "-F", "/dev/null", "-T", "-p", str(c["port"]), "-i", str(identity),
               "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "UpdateHostKeys=no", "-o", "ClearAllForwardings=yes", "-o", "ControlMaster=no",
               "-o", "ControlPath=none", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=2", c["user"] + "@" + c["host"], python + " -I -S -B -"]
    out = ROOT / "results" / ("step1-" + args.phase + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, mode=0o700)
    result = subprocess.run(command, input=source, capture_output=True, timeout=60)
    (out / "stdout.json").write_bytes(result.stdout)
    (out / "stderr.log").write_bytes(result.stderr)
    valid = False
    if result.returncode == 0:
        try:
            valid = isinstance(json.loads(result.stdout), dict)
        except ValueError:
            pass
    report = {"schema": "songplus-readonly-inspection/v1", "phase": args.phase,
              "collected_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_sha256": hashlib.sha256(source).hexdigest(), "exit_code": result.returncode,
              "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(), "valid_json": valid,
              "device_files_written_by_collector": False, "firmware_flash": False,
              "ipc_reader_registration": False,
              "note": "SSH/system logging and normal running services may update their own state; collectors never publish control messages."}
    (out / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report_directory": str(out), "exit_code": result.returncode, "valid_json": valid}))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
