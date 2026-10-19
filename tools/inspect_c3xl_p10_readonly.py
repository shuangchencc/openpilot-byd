#!/usr/bin/env python3
"""Explicit passive P10 hardware inspection. Preview unless --connect is given.

Uses existing SSH credentials and strict preexisting host-key validation. No
key contents are read. No remote script is installed. This is never a CI entry.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = {
    "recovery": "hardware/step1/followup/collector.py",
    "software": "hardware/p10-readonly/software_collector.py",
    "io": "hardware/p10-readonly/io_collector.py",
    "recovery-detail": "hardware/p10-readonly/recovery_detail_collector.py",
    "filesystem": "hardware/p10-readonly/filesystem_collector.py",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=COLLECTORS)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--connection", type=Path, default=ROOT / "local/c3xl-connection.json")
    args = parser.parse_args()
    source_path = COLLECTORS[args.phase]
    source = (ROOT / source_path).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if not args.connect:
        print(json.dumps({"mode": "preview_no_connection", "phase": args.phase,
                          "collector": source_path, "collector_sha256": digest}))
        return 0
    config = json.loads(args.connection.read_text())
    ipaddress.ip_address(config["host"])
    if config["user"] != "comma" or type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("Unexpected SSH endpoint")
    identity = Path(config["identity_file"])
    if not identity.is_file():
        raise ValueError("Existing SSH identity path required")
    argv = ["ssh", "-F", "/dev/null", "-T", "-p", str(config["port"]), "-i", str(identity),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no", "-o", "ClearAllForwardings=yes", "-o", "ControlMaster=no",
            "-o", "ControlPath=none", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2", config["user"] + "@" + config["host"],
            "/usr/bin/python3 -I -S -B -"]
    out = ROOT / "results" / ("p10-hardware-" + args.phase + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    out.mkdir(mode=0o700)
    (out / "collector.py").write_bytes(source)
    (out / "entry.py").write_bytes(Path(__file__).read_bytes())
    started = datetime.now(timezone.utc).isoformat()
    receipt = {"schema": "songplus-p10-readonly-receipt/v1", "phase": args.phase,
               "started_at_utc": started, "collector": source_path, "collector_sha256": digest,
               "entry_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "host_key_check": "StrictHostKeyChecking=yes against preexisting known_hosts",
               "target_writes_requested": False, "services_controlled": False,
               "panda_control_io": False, "can_tx": False, "profile_binding_changed": False,
               "note": "SSH and existing services may log/update their own state; snapshots are non-atomic."}
    try:
        process = subprocess.run(argv, input=source, capture_output=True, timeout=60)
        stdout, stderr = process.stdout, process.stderr
        try:
            valid = process.returncode == 0 and isinstance(json.loads(stdout), dict)
        except (ValueError, UnicodeError):
            valid = False
        receipt.update(exit_code=process.returncode, valid_json=valid, status="COLLECTED" if valid else "FAIL")
    except subprocess.TimeoutExpired as error:
        stdout, stderr = error.stdout or b"", error.stderr or b""
        receipt.update(valid_json=False, status="TIMEOUT")
    (out / "stdout.json").write_bytes(stdout)
    (out / "stderr.log").write_bytes(stderr)
    receipt.update(completed_at_utc=datetime.now(timezone.utc).isoformat(),
                   stdout_sha256=hashlib.sha256(stdout).hexdigest(), stderr_sha256=hashlib.sha256(stderr).hexdigest())
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"directory": str(out), "status": receipt["status"]}))
    return 0 if receipt["valid_json"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
