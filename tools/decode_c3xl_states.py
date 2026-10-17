#!/usr/bin/env python3
"""Decode already-copied legacy state messages on the host; never connects."""
import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fields(name, value):
    if name == "pandaStates":
        return [{"pandaType": str(p.pandaType), "ignitionLine": bool(p.ignitionLine),
                 "ignitionCan": bool(p.ignitionCan), "uptime": int(p.uptime),
                 "harnessStatus": str(p.harnessStatus), "safetyModel": str(p.safetyModel),
                 "safetyParam": int(p.safetyParam), "controlsAllowed": bool(p.controlsAllowed),
                 "heartbeatLost": bool(p.heartbeatLost), "faultStatus": str(p.faultStatus)} for p in value]
    if name == "managerState":
        return [{"name": str(p.name), "running": bool(p.running),
                 "shouldBeRunning": bool(p.shouldBeRunning), "exitCode": int(p.exitCode)} for p in value.processes]
    return {"deviceType": str(value.deviceType), "started": bool(value.started),
            "memoryUsagePercent": int(value.memoryUsagePercent), "gpuUsagePercent": int(value.gpuUsagePercent),
            "cpuTempC": list(value.cpuTempC), "gpuTempC": list(value.gpuTempC),
            "thermalStatus": str(value.thermalStatus)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--schema-dir", required=True, type=Path,
                        help="Explicit local legacy cereal schema directory; matched against remote hashes")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists")
    raw_bytes = args.input.read_bytes()
    if len(raw_bytes) > 8 * 1024 * 1024:
        raise ValueError("Oversized state snapshot")
    raw = json.loads(raw_bytes)
    tree = ast.parse((ROOT / "hardware/step1/collectors/messages.py").read_text())
    expected = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "EXPECTED_FILES" for t in n.targets))
    if raw["layout_and_schema_hashes_verified"] != expected:
        raise ValueError("Unexpected layout/schema declaration")
    schema_hashes = {}
    for source, digest in expected.items():
        prefix = "/data/openpilot/cereal/"
        if source.startswith(prefix):
            relative = source[len(prefix):]
            path = args.schema_dir / relative
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Legacy schema mismatch: " + relative)
            schema_hashes[relative] = digest
    # Parse schemas only. Do not import the archived cereal or target binaries.
    import capnp
    schema = capnp.load(str(args.schema_dir / "log.capnp"))
    samples = {}
    all_fresh = True
    for name in ("deviceState", "pandaStates", "managerState"):
        selected = []
        for sample in raw["samples"][name]:
            frame = base64.b64decode(sample["frame_base64"], validate=True)
            if len(frame) != sample["frame_bytes"] or len(frame) > 512 * 1024 or hashlib.sha256(frame).hexdigest() != sample["frame_sha256"]:
                raise ValueError("Frame hash/size mismatch")
            with schema.Event.from_bytes(frame, traversal_limit_in_words=1024 * 1024) as event:
                if event.which() != name:
                    raise ValueError("Unexpected event type")
                age = (sample["collected_monotonic_ns"] - event.logMonoTime) / 1e9
                selected.append({"valid": bool(event.valid), "event_monotonic_ns": event.logMonoTime,
                                 "collected_monotonic_ns": sample["collected_monotonic_ns"],
                                 "age_seconds": round(age, 6), "frame_sha256": sample["frame_sha256"],
                                 "fields": fields(name, getattr(event, name))})
        fresh = (len(selected) == 2 and selected[1]["event_monotonic_ns"] > selected[0]["event_monotonic_ns"]
                 and all(s["valid"] and 0 <= s["age_seconds"] <= 2 for s in selected))
        all_fresh &= fresh
        samples[name] = {"fresh_and_advancing": fresh, "samples": selected}
    out = {"schema": "songplus-readonly-state-summary/v1", "collected_utc": raw["collected_utc"],
           "method": raw["method"], "raw_snapshot_sha256": hashlib.sha256(raw_bytes).hexdigest(),
           "decoder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           "local_parser": {"package": "pycapnp", "version": capnp.__version__},
           "legacy_schema_sha256": schema_hashes, "freshness_verified": all_fresh,
           "freshness_rule": "Two valid frames per service, increasing event.logMonoTime, each captured within 0..2 seconds of its device monotonic timestamp",
           "services": samples, "device_files_written_by_collector": False, "ipc_reader_registration": False,
           "scope": "Whitelisted existing state metadata only; no CAN/vehicle protocol, camera image or new target runtime validation"}
    args.output.write_text(json.dumps(out, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "freshness_verified": all_fresh}))
    return 0 if all_fresh else 1


if __name__ == "__main__":
    raise SystemExit(main())
