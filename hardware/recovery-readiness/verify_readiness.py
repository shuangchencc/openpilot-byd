"""Offline, read-only verification of this preparation bundle; never connects.

External inputs are only individually named small historical metadata. This is
not a recovery, deployment, firmware, target-runtime, or vehicle acceptance gate.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    require(not path.is_symlink() and path.is_file(), "regular non-symlink input required")
    require(0 < path.stat().st_size <= 2 * 1024 * 1024, "small input budget exceeded")
    return path.read_bytes()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verify():
    index_raw = read(HERE / "evidence-index.json")
    index = json.loads(index_raw)
    seen = set()
    for item in index["files"]:
        path = item["path"]
        require(path not in seen and not Path(path).is_absolute() and ".." not in Path(path).parts,
                "duplicate or escaping artifact path")
        seen.add(path)
        raw = read(ROOT / path)
        require(len(raw) == item["bytes"] and sha(raw) == item["sha256"], "artifact mismatch: " + path)
    matrix = json.loads(read(HERE / "recovery-requirements.json"))
    for source in matrix["sources"]:
        raw = read(ROOT / source["path"])
        require(len(raw) == source["bytes"] and sha(raw) == source["sha256"], "historical metadata mismatch")

    ev = HERE / "evidence/20261020"
    for label, original_collector in (("io", None), ("supervisor", None),
                                      ("gpt-headers", ev / "gpt-header-collector-original.txt"),
                                      ("gpt-headers-bounded", None)):
        receipt = json.loads(read(ev / (label + "-receipt.json")))
        require(sha(read(ev / (label + ".json"))) == receipt["stdout_sha256"], "capture hash mismatch")
        source = original_collector or ROOT / receipt["collector"]
        require(sha(read(source)) == receipt["collector_sha256"], "collector provenance mismatch")
        entry = ROOT / "tools" / ("inspect_c3xl_p10_readonly.py" if label == "io" else "inspect_c3xl_recovery_readiness.py")
        require(sha(read(entry)) == receipt["entry_sha256"], "entry provenance mismatch")
        require(receipt["exit_code"] == 0 and receipt["status"] in ("COLLECTED", "COLLECTED_PASSIVE_ONLY"), "capture failed")
        for field in ("target_writes_requested", "services_controlled", "can_tx", "profile_binding_changed"):
            require(receipt[field] is False, "boundary changed")
        require(receipt["panda_control_io" if label == "io" else "usb_spi_control_io"] is False, "control IO requested")

    supervisor = json.loads(read(ev / "supervisor.json"))
    blocks = supervisor["storage"]["block_devices"]
    luns = {item["device_at_observation"].split("/")[-1]: item for item in matrix["object_coverage"]["luns"]}
    require(len(luns) == 6, "six LUNs required")
    for name, lun in luns.items():
        require(blocks[name]["capacity_bytes"] == lun["bytes"], "LUN capacity drift")
        require(int(blocks[name]["queue/logical_block_size"]) == lun["sector_bytes"], "sector drift")
        require(lun["current_full_lun_capture_sha256"] is None, "metadata must not become content backup")
    partitions = matrix["object_coverage"]["partitions"]
    require(len(partitions) == 70 and len({p["id"] for p in partitions}) == 70, "partition inventory")
    for part in partitions:
        lun = luns[part["lun"]]
        size = (part["last_lba"] - part["first_lba"] + 1) * lun["sector_bytes"]
        require(part["first_lba"] >= 0 and size == part["bytes"] and (part["last_lba"] + 1) * lun["sector_bytes"] <= lun["bytes"], "partition range")
        require(blocks[part["device_at_observation"].split("/")[-1]]["capacity_bytes"] == size, "current partition size drift")
        require(part["current_capture"] is None and part["current_content_sha256"] is None, "uncollected object falsely complete")

    stages = {s["id"]: s["prerequisites"] for s in matrix["stages"]}
    def ancestors(stage, path=()):
        require(stage not in path and stage in stages, "dependency cycle or missing stage")
        result = set()
        for dep in stages[stage]:
            result.add(dep)
            result.update(ancestors(dep, path + (stage,)))
        return result
    for stage in stages:
        ancestors(stage)
    require(ancestors("R2") == {"R0", "R1"}, "host capture depends on unrelated recovery/exclusivity")

    first = json.loads(read(ev / "gpt-headers.json"))
    fixed = json.loads(read(ev / "gpt-headers-bounded.json"))
    require(first["sde"] == fixed["sde"], "GPT changed across observed capture windows")
    analysis = json.loads(read(HERE / "gpt-analysis.json"))
    current = analysis["current_header_validation"]
    require(current["status"] == "EXACT_FULL_SECTOR_HASH_MATCH", "exact header analysis unavailable")
    require(current["collected_utc"] == fixed["collected_utc"], "analysis uses obsolete sampling")
    crc = current["backup_crc_only_validation"]
    require(crc["changed_byte_offsets_within_header_sector"] == [16, 17, 18, 19, 88, 89, 90, 91], "unexpected CRC difference")
    require(crc["all_other_bytes_preserved"] and not crc["payload_saved"] and not crc["device_write_authorized"], "CRC analysis boundary")
    require(not analysis["deployable"] and not analysis["device_writes_authorized"], "analysis must remain nondeployable")

    profile = json.loads(read(ROOT / "lifecycle_maintenance/profiles/unbound.json"))
    require(profile["binding_status"] == "UNBOUND", "real profile changed")
    for field in ("device_access_enabled", "can_tx_enabled", "firmware_install_enabled"):
        require(profile[field] is False, "permission unexpectedly enabled")
    require(sha(read(ROOT / "lifecycle_maintenance/lock.json")) == "64fe409526045fd23ef5b80fb6b80666af42ce32cc28bb915358d3f793af8611", "P10 lock changed")
    return {"schema": "songplus-recovery-readiness-offline-check/v1", "status": "PASS_LOCAL_PREPARATION_ONLY",
            "evidence_index_sha256": sha(index_raw), "indexed_files_verified": len(seen),
            "historical_small_sources_verified": len(matrix["sources"]), "passive_capture_receipts_verified": 4,
            "current_lun_capacities_compared": len(luns), "current_partition_capacities_compared": len(partitions),
            "stage_dependencies_acyclic": True, "profile": "UNBOUND", "current_content_backups": 0,
            "recovery_exercises": 0, "target_services_controlled": False,
            "live_board_io": False, "deployment_authorized": False}


if __name__ == "__main__":
    try:
        print(json.dumps(verify(), indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "message": str(error)}))
        raise SystemExit(1)
