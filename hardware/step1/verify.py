#!/usr/bin/env python3
"""Verify packaged Step 1 evidence locally; never connect or execute collectors."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads((ROOT / path).read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    manifest = read("hardware/step1/evidence-manifest.json")
    seen = set()
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        require(not relative.is_absolute() and ".." not in relative.parts, "Evidence path must be repository-relative")
        path = ROOT / relative
        require(path.resolve().is_relative_to(ROOT.resolve()) and not path.is_symlink(), "Evidence path escapes project")
        require(entry["path"] not in seen, "Duplicate evidence path")
        seen.add(entry["path"])
        data = path.read_bytes()
        require(len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"], "Evidence hash mismatch: " + entry["path"])
    expected = {str(p.relative_to(ROOT)) for p in (ROOT / "hardware/step1/evidence").glob("*.json")}
    require(seen == expected, "Evidence manifest must cover exactly the packaged JSON evidence")

    requirements = read("hardware/step1/official-requirements.json")
    require(requirements["baseline_commits"]["openpilot"] == read("upstream-lock.json")["selection"]["commit"], "Official pin differs")
    anchors = 0
    for item in requirements["items"]:
        for a in item["anchors"]:
            p = ROOT / a["local_path"]
            require(p.resolve().is_relative_to((ROOT / "upstream-openpilot").resolve()), "Anchor outside official checkout")
            data = p.read_bytes()
            require(hashlib.sha256(data).hexdigest() == a["sha256"], "Official source anchor hash differs")
            require(1 <= a["start_line"] <= a["end_line"] <= len(data.splitlines()), "Anchor line range invalid")
            anchors += 1
    for phase in ("system", "details", "recovery", "messages"):
        receipt = read("hardware/step1/evidence/live-" + phase + "-receipt.json")
        require(receipt["exit_code"] == 0 and receipt["valid_json"], "Collection did not succeed: " + phase)
        require(receipt["device_files_written_by_collector"] is False and receipt["firmware_flash"] is False,
                "Unexpected device write declaration")
        require(receipt["ipc_reader_registration"] is False, "Unexpected IPC reader registration")
        source = ROOT / "hardware/step1/collectors" / (phase + ".py")
        require(hashlib.sha256(source.read_bytes()).hexdigest() == receipt["source_sha256"], "Collector changed since recorded execution: " + phase)
        if phase != "messages":
            data = (ROOT / "hardware/step1/evidence" / ("live-" + phase + ".json")).read_bytes()
            require(hashlib.sha256(data).hexdigest() == receipt["stdout_sha256"], "Collection stdout hash differs")
    states = read("hardware/step1/evidence/live-messages.json")
    receipt = read("hardware/step1/evidence/live-messages-receipt.json")
    require(states["raw_snapshot_sha256"] == receipt["stdout_sha256"], "Decoded states do not bind to receipt")
    require(states["decoder_sha256"] == hashlib.sha256((ROOT / "tools/decode_c3xl_states.py").read_bytes()).hexdigest(),
            "Decoder changed since recorded interpretation")
    require(states["freshness_verified"] is True, "No complete fresh message evidence")
    require(states["ipc_reader_registration"] is False, "Unexpected IPC mutation declaration")
    require(set(states["services"]) == {"deviceState", "pandaStates", "managerState"}, "Missing or unexpected state service")
    for value in states["services"].values():
        s = value["samples"]
        require(len(s) == 2 and s[1]["event_monotonic_ns"] > s[0]["event_monotonic_ns"], "No advancing message pair")
        require(all(e["valid"] and 0 <= e["age_seconds"] <= 2 for e in s), "State evidence not fresh at collection")
        require(all(abs((e["collected_monotonic_ns"] - e["event_monotonic_ns"]) / 1e9 - e["age_seconds"]) <= 0.000001 for e in s),
                "Recorded message age does not match its clocks")
    disks = read("hardware/step1/evidence/live-recovery.json")["disks"]
    require(len(disks) == 6, "Missing disk evidence")
    require(all(d["primary"]["header_crc_valid"] and d["secondary"]["header_crc_valid"] and d["tables_identical"] for d in disks.values()),
            "Unexpected GPT structure; review observed state")
    require(disks["sde"]["secondary"]["entries_crc_valid"] is False, "Recorded known sde anomaly changed; review required")
    require(all(d[side]["entries_crc_valid"] for name, d in disks.items() for side in ("primary", "secondary")
                if (name, side) != ("sde", "secondary")), "Unexpected additional GPT entry CRC anomaly")
    print(json.dumps({"status": "PASS", "evidence_files": len(seen), "official_source_anchors": anchors,
                      "remote_collectors_executed_by_this_verifier": False, "network_used": False,
                      "meaning": "Recorded evidence integrity and scope only; known GPT anomaly remains; hardware compatibility and recovery are not established"}, indent=2))


if __name__ == "__main__":
    main()
