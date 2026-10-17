#!/usr/bin/env python3
"""Verify frozen follow-up evidence locally; never import/run the collector."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "hardware/step1/followup"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def verify_local_record(record, relative):
    path_fragment = Path(relative)
    require(not path_fragment.is_absolute() and ".." not in path_fragment.parts, "Invalid local source path")
    path = ROOT / path_fragment
    require(path.resolve().is_relative_to(ROOT.resolve()), "Local source outside project")
    require(digest(path) == record["sha256"], "Local report source hash mismatch: " + relative)
    if "bytes" in record:
        require(path.stat().st_size == record["bytes"], "Local report source size mismatch: " + relative)


def verify_explicit_local_sources(value):
    """Verify project-bound citations; never resolve original private archives."""
    count = 0
    if isinstance(value, dict):
        relative = None
        for key in ("path_relative_to_project", "path_relative_to_repository"):
            if key in value:
                relative = value[key]
                break
        if relative is None:
            for key in ("path_relative_to_original_workspace", "archive_relative_to_original_workspace", "path"):
                original = value.get(key)
                if isinstance(original, str) and original.startswith("songplus-port-lab/"):
                    relative = original.removeprefix("songplus-port-lab/")
                    break
        if relative is not None:
            verify_local_record(value, relative)
            count += 1
        for child in value.values():
            count += verify_explicit_local_sources(child)
    elif isinstance(value, list):
        for child in value:
            count += verify_explicit_local_sources(child)
    return count


def verify_reports(evidence, disks):
    pin = read(ROOT / "upstream-lock.json")["selection"]["commit"]
    profile = read(PACKAGE / "hardware-profile.json")
    recovery = read(PACKAGE / "recovery-findings.json")
    require(profile["official_openpilot_commit"] == pin, "Hardware report pin differs")
    anchors = profile["official_source_anchors"]
    require(len(anchors) == 18, "Official hardware anchor coverage changed")
    for anchor in anchors:
        require(anchor["repository"] == "commaai/openpilot" and anchor["commit"] == pin,
                "Official anchor repository/pin differs")
        relative = Path(anchor["path"])
        require(not relative.is_absolute() and ".." not in relative.parts, "Invalid official anchor path")
        path = ROOT / "upstream-openpilot" / relative
        require(path.resolve().is_relative_to((ROOT / "upstream-openpilot").resolve()), "Anchor outside official tree")
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == anchor["sha256"], "Official anchor source changed")
        require(1 <= anchor["start_line"] <= anchor["end_line"] <= len(data.splitlines()), "Official anchor lines invalid")
    for record in profile["existing_evidence"]:
        verify_local_record(record, record["path"])
    local_sources = verify_explicit_local_sources(profile) + verify_explicit_local_sources(recovery)

    flags = recovery["flag_decoder"]
    library = evidence["system_files"]["/usr/lib/aarch64-linux-gnu/libabctl.so.0.0.0"]
    require(flags["library_sha256"] == library["sha256"] and flags["live_library_hash_matches"] is True,
            "Static flag decoder does not match observed installed library")
    require(flags["target_program_or_library_executed"] is False and flags["priority_retry_decoded"] is False,
            "Unsupported flag decoding scope")
    require(flags["python_expression"] == {"active": "(attributes >> 50) & 1", "success": "(attributes >> 54) & 1",
                                           "unbootable": "(attributes >> 55) & 1"}, "Flag bit interpretation changed")
    current = flags["current_values"]
    require(len(current) == 2 and {v["partition"] for v in current} == {"boot_a", "boot_b"},
            "Flag values must cover only boot_a and boot_b")
    boot_parts = {p["name"]: p for p in disks["sde"]["primary"]["partitions"] if p["name"] in ("boot_a", "boot_b")}
    for value in current:
        recorded = boot_parts[value["partition"]]["attributes_hex"]
        require(value["attributes_hex"] == recorded, "Flag report differs from live GPT")
        attrs = int(recorded, 16)
        for field, bit in (("active_bit50", 50), ("success_bit54", 54), ("unbootable_bit55", 55)):
            require(value[field] == (attrs >> bit) & 1, "Flag report bit value differs")
    return len(anchors), local_sources


def verify_manifest():
    manifest = read(PACKAGE / "manifest.json")
    seen = set()
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        require(not relative.is_absolute() and ".." not in relative.parts, "Invalid manifest path")
        path = ROOT / relative
        require(path.resolve().is_relative_to(PACKAGE.resolve()) and not path.is_symlink(),
                "Manifest file outside follow-up package")
        require(entry["path"] not in seen, "Duplicate manifest entry")
        seen.add(entry["path"])
        require(path.stat().st_size == entry["bytes"] and digest(path) == entry["sha256"],
                "Manifest hash/size mismatch: " + entry["path"])
    expected = {str(p.relative_to(ROOT)) for p in PACKAGE.rglob("*")
                if p.is_file() and p != PACKAGE / "manifest.json"
                and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    require(seen == expected, "Manifest must cover exactly the follow-up package")
    return len(seen)


def main():
    file_count = verify_manifest()
    evidence_path = PACKAGE / "live-evidence.json"
    evidence, receipt = read(evidence_path), read(PACKAGE / "live-receipt.json")
    require(evidence["schema"] == "songplus-c3xl-passive-followup/v1", "Unexpected evidence schema")
    require(receipt["schema"] == "songplus-readonly-followup-receipt/v1", "Unexpected receipt schema")
    require(receipt["exit_code"] == 0 and receipt["valid_json"] is True, "Collection failed")
    require(receipt["device_files_written_by_collector"] is False and receipt["device_controls_called"] is False,
            "Unexpected device mutation declaration")
    require(receipt["source_sha256"] == digest(PACKAGE / "collector.py"), "Executed collector changed")
    require(receipt["runner_sha256"] == digest(ROOT / "tools/inspect_c3xl_followup.py"), "Executed runner changed")
    require(receipt["stdout_sha256"] == digest(evidence_path), "Raw evidence differs from receipt")

    # The earlier Step 1 files remain immutable evidence, not a moving baseline.
    prior_manifest = read(ROOT / "hardware/step1/evidence-manifest.json")
    for item in prior_manifest["files"]:
        path = ROOT / item["path"]
        require(path.stat().st_size == item["bytes"] and digest(path) == item["sha256"], "Earlier evidence changed")
    for phase in ("system", "details", "messages", "recovery"):
        old_receipt = read(ROOT / "hardware/step1/evidence" / ("live-" + phase + "-receipt.json"))
        require(old_receipt["source_sha256"] == digest(ROOT / "hardware/step1/collectors" / (phase + ".py")),
                "Earlier collector changed")
    prior_disks = read(ROOT / "hardware/step1/evidence/live-recovery.json")["disks"]
    disks = evidence["gpt"]
    require(set(disks) == {"sda", "sdb", "sdc", "sdd", "sde", "sdf"}, "Missing disk evidence")
    partition_nodes = {}
    for name, disk in disks.items():
        require(disk["sector_bytes"] in (512, 4096), "Invalid sector size")
        require(disk["bytes"] == prior_disks[name]["bytes"], "Disk capacity differs from earlier evidence")
        require(disk["primary"]["entries_sha256"] == disk["secondary"]["entries_sha256"], "GPT copies differ")
        require(disk["primary"]["partitions"] == disk["secondary"]["partitions"], "GPT decoded copies differ")
        for side in ("primary", "secondary"):
            header = disk[side]
            require(header["header_crc_valid"] is True, "GPT header CRC failed")
            require(header["entries_crc_valid"] is ((name, side) != ("sde", "secondary")),
                    "Known GPT entry CRC anomaly changed")
            require(header["entries_sha256"] == prior_disks[name][side]["entries_sha256"], "GPT entries changed")
            require(disk["sector_bytes"] < header["read_bytes"] <= disk["sector_bytes"] + 2 * 1024 * 1024,
                    "GPT read exceeds declared metadata bound")
        for part in disk["primary"]["partitions"]:
            require(0 <= part["first_lba"] <= part["last_lba"] < disk["bytes"] // disk["sector_bytes"],
                    "Partition extent outside disk")
            require(re.fullmatch(r"0x[0-9a-f]{16}", part["attributes_hex"]) is not None, "Invalid GPT attributes")
            node = "/dev/" + name + str(part["partition"])
            partition_nodes.setdefault(part["name"], set()).add(node)

    links = evidence["partition_links"]
    for label in ("dsp_a", "dsp_b", "modem_a", "modem_b", "system_a", "system_b", "boot_a", "boot_b"):
        require(links["/dev/disk/by-partlabel/" + label] in partition_nodes[label], "Partition link/GPT mismatch")
    require("androidboot.slot_suffix=_b" in evidence["boot_parameters"], "Recorded current slot changed")
    require("root=" + links["/dev/disk/by-partlabel/system_b"] in evidence["boot_parameters"], "Root/slot/link mismatch")
    fstab = {item["text"].split()[1]: item["text"].split()[0]
             for item in evidence["system_files"]["/etc/fstab"]["selected_lines"]}
    mounts = {line.split()[1]: line.split()[0] for line in evidence["mounts"]}
    for mount, label in (("/dsp", "dsp_a"), ("/firmware", "modem_a")):
        require(fstab[mount] in links, "Missing fstab source link")
        require(links[fstab[mount]] == mounts[mount] and mounts[mount] in partition_nodes[label],
                "fstab/link/mount/GPT chain differs")

    # These are cached software enumeration/configuration counts, never a BOM
    # or evidence that cameras, a SIM, a recovery cable, or a radio link work.
    usb = evidence["usb_devices"]
    panda = [d for d in usb if (d.get("idVendor"), d.get("idProduct")) == ("bbaa", "ddcc")]
    cellular_usb = [d for d in usb if (d.get("idVendor"), d.get("idProduct")) == ("2c7c", "0125")]
    require(len(panda) == 1 and len(cellular_usb) == 1, "Cached USB enumeration differs")
    drivers = {Path(i["driver"]).name for i in cellular_usb[0]["interfaces"] if isinstance(i.get("driver"), str)}
    require({"option", "qmi_wwan"} <= drivers, "Cached cellular USB driver bindings differ")
    configured_sensors = [n for n in evidence["camera_dt"] if "qcom,cam-sensor" in n.get("compatible", "").split("\0")]
    require({tuple(n["cell-index_u32"]) for n in configured_sensors} == {(0,), (1,), (2,), (3,)},
            "Recorded configured camera slots differ")
    anchor_count, local_sources = verify_reports(evidence, disks)

    result = read(PACKAGE / "RESULT.json")
    require(isinstance(result["status"], str) and bool(result["status"]), "Missing report status")
    require(result["official_commit"] == read(ROOT / "upstream-lock.json")["selection"]["commit"], "Official baseline differs")
    require(result["recovery_verified"] is False and result["deployment_ready"] is False, "Unsupported readiness claim")
    require(result["no_driver_camera_user_reported"] is True, "User hardware clarification missing")
    require(result["roles_changed_by_collector"] is False, "Unexpected USB role mutation claim")
    print(json.dumps({"status": "PASS", "package_files": file_count, "disk_headers_checked": 12,
                      "known_invalid_entry_crc": "sde.secondary", "fstab_link_mount_chains_checked": 2,
                      "cached_usb_nodes": len(usb), "configured_camera_sensor_slots": len(configured_sensors),
                      "official_hardware_source_anchors": anchor_count, "project_bound_report_sources": local_sources,
                      "boot_attribute_pairs_recomputed": 2, "static_target_code_executed": False,
                      "network_used": False, "collector_executed_by_verifier": False,
                      "meaning": "Recorded evidence integrity only; software node counts do not prove physical cameras/SIM or operational recovery"}, indent=2))


if __name__ == "__main__":
    main()
