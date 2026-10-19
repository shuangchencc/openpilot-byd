"""Local GPT evidence analysis; JSON summaries only, never binary output.

Reads bounded regular archive/evidence files. No device paths, networking,
subprocesses, writes, repair commands, or automatic deployment are implemented.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import struct
import zlib

from gpt_header_collector import HEADER_FIELDS, describe_table, parse_header, sha, validate_collection_scope

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "hardware/p10-readonly/evidence/20261019"
ARCHIVE = ROOT.parent / "migration-audit/recovery-backup-20261017/gpt"
ARCHIVE_HASHES = {
    "sde-primary.bin": "d39aaa4edff5023eaf7ac6d00e1f1ff18e5cc6fb1130962ccffd122dda3e2d7c",
    "sde-declared_backup.bin": "96f40f7d7addd094dd968777c6cdaed8119d1c3c165f504bd1ad1c157e7d9fb1",
    "sde-physical_end.bin": "96f40f7d7addd094dd968777c6cdaed8119d1c3c165f504bd1ad1c157e7d9fb1",
    "sde-primary_entries.bin": "4f43d094ebcbcebdced44488a8b96070727f3c77e4ebf1c8ff434714b55d173b",
    "sde-declared_backup_entries.bin": "4f43d094ebcbcebdced44488a8b96070727f3c77e4ebf1c8ff434714b55d173b",
    "sde-physical_end_entries.bin": "4f43d094ebcbcebdced44488a8b96070727f3c77e4ebf1c8ff434714b55d173b",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bounded_regular(path, maximum=262144):
    require(stat.S_ISREG(Path(path).lstat().st_mode), "input must be a nonsymlink regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        status = os.fstat(fd)
        require(stat.S_ISREG(status.st_mode), "input must be a regular file")
        require(status.st_size <= maximum, "input size limit")
        chunks, remaining = [], maximum + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        require(len(data) <= maximum, "input grew beyond limit")
        return data
    finally:
        os.close(fd)


def fingerprints(table, count, size):
    require(1 <= count <= 16384 and 128 <= size <= 4096 and size % 128 == 0,
            "entry dimensions")
    require(len(table) == count * size <= 65536, "table dimensions or budget")
    used, unused = [], bytearray()
    for index in range(count):
        entry = table[index * size:(index + 1) * size]
        if entry[:16] == bytes(16):
            unused.extend(struct.pack("<I", index)); unused.extend(entry)
            continue
        name = entry[56:128].decode("utf-16le", "strict").rstrip("\0")
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,36}", name), "entry name")
        used.append({"partition": index + 1, "name": name,
                     "type_guid_sha256": sha(entry[:16]),
                     "unique_guid_sha256": sha(entry[16:32]),
                     "lba_sha256": sha(entry[32:48]),
                     "attributes_hex": "0x%016x" % struct.unpack_from("<Q", entry, 48)[0],
                     "name_bytes_sha256": sha(entry[56:128]),
                     "extension_sha256": sha(entry[128:]), "entry_sha256": sha(entry)})
    return {"entries_sha256": sha(table), "count": count, "size": size, "used": used,
            "unused_indexed_entries_sha256": sha(unused), "raw_guids_emitted": False}


def reconstruct_entries(old, expected):
    count, size = expected["count"], expected["size"]
    original = fingerprints(old, count, size)
    require(len({r["name"] for r in original["used"]}) == len(original["used"]), "duplicate archive names")
    require(len({r["partition"] for r in expected["used"]}) == len(expected["used"]), "duplicate expected indexes")
    require(len({r["name"] for r in expected["used"]}) == len(expected["used"]), "duplicate expected names")
    old_rows = {r["partition"]: r for r in original["used"]}
    names = {r["name"]: r for r in original["used"]}
    require(set(old_rows) == {r["partition"] for r in expected["used"]}, "occupied indexes changed")
    candidate = bytearray(old)
    attributes, swaps = [], []
    for row in expected["used"]:
        previous = old_rows[row["partition"]]
        for field in ("name", "unique_guid_sha256", "lba_sha256", "name_bytes_sha256", "extension_sha256"):
            require(row[field] == previous[field], "non-allowed entry field changed: " + field)
        offset = (row["partition"] - 1) * size
        if row["type_guid_sha256"] != previous["type_guid_sha256"]:
            name = row["name"]
            require(name.endswith(("_a", "_b")), "type change outside A/B pair")
            other_name = name[:-1] + ("b" if name.endswith("_a") else "a")
            require(other_name in names, "missing opposite slot")
            other = names[other_name]
            require(other["type_guid_sha256"] == row["type_guid_sha256"], "type hash not opposite slot")
            donor = (other["partition"] - 1) * size
            candidate[offset:offset + 16] = old[donor:donor + 16]
            swaps.append({"partition": row["partition"], "opposite_partition": other["partition"], "name": name})
        if row["attributes_hex"] != previous["attributes_hex"]:
            value = int(row["attributes_hex"], 16)
            require(0 <= value < 2 ** 64, "attribute range")
            struct.pack_into("<Q", candidate, offset + 48, value)
            attributes.append({"partition": row["partition"], "name": row["name"],
                               "old": previous["attributes_hex"], "observed": row["attributes_hex"]})
    pairs = {(x["partition"], x["opposite_partition"]) for x in swaps}
    require(all((b, a) in pairs for a, b in pairs), "type changes are not reciprocal swaps")
    actual = fingerprints(candidate, count, size)
    require(actual == expected, "reconstructed whole table or component fingerprint mismatch")
    changed = [i for i, (a, b) in enumerate(zip(old, candidate)) if a != b]
    require(all(i % size < 16 or 48 <= i % size < 56 for i in changed), "change outside allowed bytes")
    return bytes(candidate), {
        "status": "EXACT_HASH_MATCH", "entries_bytes": len(candidate),
        "entries_count": count, "entry_size": size, "used_entries": len(actual["used"]),
        "unused_entries": count - len(actual["used"]),
        "archive_entries_sha256": sha(old), "observed_entries_sha256": sha(candidate),
        "observed_entries_crc32": zlib.crc32(candidate) & 0xffffffff,
        "attribute_changes": attributes, "attribute_changed_entries": len(attributes),
        "type_guid_swap_pairs": sorted([[a, b] for a, b in pairs if a < b]),
        "changed_byte_count": len(changed), "unchanged_unique_guid_lba_name_extension": True,
        "unused_indexed_entries_sha256": actual["unused_indexed_entries_sha256"],
        "payload_saved": False,
    }


def validate_layout(table, header, observed_partitions=None):
    count, size = header["entries_count"], header["entry_size"]
    rows = fingerprints(table, count, size)["used"]
    ranges, guids = [], set()
    for row in rows:
        offset = (row["partition"] - 1) * size
        first, last, attrs = struct.unpack_from("<QQQ", table, offset + 32)
        require(header["first_usable_lba"] <= first <= last <= header["last_usable_lba"], "partition outside usable bounds")
        unique = bytes(table[offset + 16:offset + 32])
        require(unique != bytes(16) and unique not in guids, "empty or duplicate unique GUID")
        guids.add(unique)
        ranges.append({"partition": row["partition"], "name": row["name"],
                       "first_lba": first, "last_lba": last, "attributes_hex": f"0x{attrs:016x}"})
    ordered = sorted(ranges, key=lambda r: r["first_lba"])
    require(all(a["last_lba"] < b["first_lba"] for a, b in zip(ordered, ordered[1:])), "partition overlap")
    if observed_partitions is not None:
        require(ranges == observed_partitions, "range/attribute mismatch against recovery sample")
    return {"partition_count": len(rows), "ranges_nonoverlapping": True,
            "within_header_usable_bounds": True, "unique_nonzero_partition_guids": True,
            "first_partition_lba": ordered[0]["first_lba"], "last_partition_lba": ordered[-1]["last_lba"],
            "partitions": ranges}


def validate_header_pair(primary, secondary, disk_bytes):
    sector = primary["sector_bytes"]
    require(sector == secondary["sector_bytes"], "header sector mismatch")
    end = disk_bytes // sector - 1
    require(primary["current_lba"] == primary["observed_lba"] == secondary["alternate_lba"] == 1, "primary/alternate mismatch")
    require(secondary["current_lba"] == secondary["observed_lba"] == primary["alternate_lba"] == end, "secondary/alternate mismatch")
    for field in ("revision", "header_size", "first_usable_lba", "last_usable_lba", "disk_guid_sha256", "entries_count", "entry_size"):
        require(primary[field] == secondary[field], "paired metadata mismatch: " + field)
    require(primary["revision"] == 65536 and primary["reserved"] == secondary["reserved"] == 0, "unsupported GPT revision or reserved")
    blocks = (primary["entries_bytes"] + sector - 1) // sector
    require(1 < primary["entries_lba"] and primary["entries_lba"] + blocks <= primary["first_usable_lba"], "primary metadata overlaps usable area")
    require(secondary["last_usable_lba"] < secondary["entries_lba"] and secondary["entries_lba"] + blocks <= end, "secondary metadata overlaps usable area/header")
    require(primary["first_usable_lba"] <= primary["last_usable_lba"], "usable range")
    return {"reciprocal_header_lbas": True, "matching_geometry_guid_digest_and_shape": True,
            "metadata_outside_usable_range": True, "disk_bytes": disk_bytes, "sector_bytes": sector,
            "first_usable_lba": primary["first_usable_lba"], "last_usable_lba": primary["last_usable_lba"]}


def crc_only_summary(header, entries, sector, disk_bytes, lba):
    original = parse_header(header, sector, disk_bytes, lba)
    require(original["header_crc_valid"], "original header CRC invalid")
    require(len(entries) == original["entries_bytes"], "entries length")
    candidate = bytearray(header)
    struct.pack_into("<I", candidate, 88, zlib.crc32(entries) & 0xffffffff)
    candidate[16:20] = bytes(4)
    struct.pack_into("<I", candidate, 16, zlib.crc32(candidate[:original["header_size"]]) & 0xffffffff)
    revised = parse_header(candidate, sector, disk_bytes, lba)
    table = describe_table(revised, entries)
    require(revised["header_crc_valid"] and table["entries_crc_valid"], "candidate CRC check")
    changed = [i for i, (a, b) in enumerate(zip(header, candidate)) if a != b]
    require(set(changed) <= set(range(16, 20)) | set(range(88, 92)), "candidate changed non-CRC bytes")
    return {"status": "IN_MEMORY_CRC_CHECK_ONLY", "original_header_sector_sha256": sha(header),
            "derived_header_sector_sha256": sha(candidate), "entries_sha256": sha(entries),
            "changed_byte_offsets_within_header_sector": changed,
            "changed_fields": ["header_crc32", "entries_crc32"],
            "original_entries_crc32_stored": original["entries_crc32_stored"],
            "derived_entries_crc32": revised["entries_crc32_stored"],
            "original_header_crc32_stored": original["header_crc32_stored"],
            "derived_header_crc32": revised["header_crc32_stored"],
            "derived_header_crc_valid": True, "derived_entries_crc_valid": True,
            "all_other_bytes_preserved": True, "payload_saved": False,
            "deployable": False, "device_write_authorized": False}


def reconstruct_header(old, observed, disk_bytes):
    # Every byte not represented by an observed numeric field is retained from
    # the old sector and checked by region hashes plus the full observed SHA.
    candidate = bytearray(old)
    for name, offset, fmt in HEADER_FIELDS:
        struct.pack_into("<" + fmt, candidate, offset, observed[name])
    parsed = parse_header(candidate, observed["sector_bytes"], disk_bytes, observed["observed_lba"])
    for key, value in parsed.items():
        require(observed[key] == value, "current header reconstruction mismatch: " + key)
    require(observed["header_stable_across_table_read"] is True and observed["header_after_sha256"] == sha(candidate), "header changed within sample")
    require(parsed["header_crc_valid"], "current header CRC invalid")
    return bytes(candidate)


def analyze(header_evidence=None, header_receipt=None):
    sources, old = [], {}
    for name, digest in ARCHIVE_HASHES.items():
        data = bounded_regular(ARCHIVE / name, 8192)
        require(sha(data) == digest, "archive hash mismatch: " + name)
        old[name] = data
        sources.append({"path": "../migration-audit/recovery-backup-20261017/gpt/" + name,
                        "bytes": len(data), "sha256": sha(data), "kind": "historical_small_raw_GPT"})
    documents = {}
    for name in ("recovery", "recovery-detail"):
        data = bounded_regular(EVIDENCE / (name + ".json"))
        receipt_data = bounded_regular(EVIDENCE / (name + ".receipt.json"))
        receipt = json.loads(receipt_data)
        require(receipt["stdout_sha256"] == sha(data) and receipt["status"] == "COLLECTED" and receipt["exit_code"] == 0, "evidence receipt mismatch")
        collector_path = ROOT / receipt["collector"]
        collector_data = bounded_regular(collector_path)
        require(sha(collector_data) == receipt["collector_sha256"], "historical collector changed")
        for path, content, kind in ((EVIDENCE / (name + ".json"), data, "historical_readonly_evidence"),
                                    (EVIDENCE / (name + ".receipt.json"), receipt_data, "receipt"),
                                    (collector_path, collector_data, "collector_source")):
            sources.append({"path": str(path.relative_to(ROOT)), "bytes": len(content), "sha256": sha(content), "kind": kind})
        documents[name] = json.loads(data)
    recovery = documents["recovery"]["gpt"]["sde"]
    detail = documents["recovery-detail"]["sde"]
    require(detail["primary"] == detail["secondary"], "current entry fingerprints differ")
    require(old["sde-primary_entries.bin"] == old["sde-declared_backup_entries.bin"] == old["sde-physical_end_entries.bin"], "historical tables differ")
    require(old["sde-declared_backup.bin"] == old["sde-physical_end.bin"], "historical backup headers differ")
    table, reconstruction = reconstruct_entries(old["sde-primary_entries.bin"], detail["primary"])
    require(reconstruction["attribute_changed_entries"] == 34 and len(reconstruction["type_guid_swap_pairs"]) == 17, "unexpected historical transition")
    sector, disk_bytes = recovery["sector_bytes"], recovery["bytes"]
    parsed = {}
    for label, name in (("primary", "sde-primary.bin"), ("secondary", "sde-physical_end.bin")):
        require(recovery[label]["entries_sha256"] == sha(table), "recovery/detail hash mismatch")
        require(recovery[label]["header_crc_valid"] is True, "sampled header CRC invalid")
        parsed[label] = parse_header(old[name], sector, disk_bytes, recovery[label]["header_lba"])
        require(parsed[label]["header_crc_valid"], "historical header CRC invalid")
    require(recovery["primary"]["entries_crc_valid"] is True and recovery["secondary"]["entries_crc_valid"] is False, "unexpected sampled CRC condition")
    historical_primary_table = describe_table(parsed["primary"], old["sde-primary_entries.bin"])
    historical_secondary_table = describe_table(parsed["secondary"], old["sde-physical_end_entries.bin"])
    require(historical_primary_table["entries_crc_valid"] is True and historical_secondary_table["entries_crc_valid"] is False, "unexpected historical CRC condition")
    pair = validate_header_pair(parsed["primary"], parsed["secondary"], disk_bytes)
    layout = validate_layout(table, parsed["primary"], recovery["primary"]["partitions"])
    require(recovery["primary"]["partitions"] == recovery["secondary"]["partitions"], "sampled partition metadata differs")
    result = {"schema": "songplus-gpt-offline-analysis/v1", "status": "EVIDENCE_ANALYSIS_ONLY",
              "raw_guids_emitted": False, "raw_payloads_saved": False,
              "device_writes_authorized": False, "recovery_ready": False, "deployable": False,
              "sources": sources, "entry_evidence_collected_utc": documents["recovery-detail"]["collected_utc"],
              "non_atomic_snapshots": True, "entries_reconstruction": reconstruction,
              "historical_header_pair_geometry": pair, "reconstructed_entries_layout": layout,
              "historical_entries_crc_status": {"primary_valid": True, "secondary_valid": False},
              "historical_backup_crc_only_validation": crc_only_summary(old["sde-physical_end.bin"], old["sde-primary_entries.bin"], sector, disk_bytes, disk_bytes // sector - 1),
              "current_header_validation": {"status": "INCOMPLETE_MISSING_CURRENT_HEADER_EVIDENCE",
                                             "current_header_bytes_reconstructed": False,
                                             "current_crc_only_validation_performed": False},
              "limits": ["Hash equality ties reconstructed bytes to the sampled evidence, not to a future device state.",
                         "Historical A/B metadata differences do not prove who changed them or why.",
                         "CRC consistency does not prove bootability, partition contents, recovery entry, or a tested rollback path.",
                         "No raw binary output or device write procedure is produced."]}
    if header_evidence is not None:
        data = bounded_regular(header_evidence)
        current = json.loads(data)
        require(header_receipt is not None, "new header evidence requires its receipt")
        receipt_data = bounded_regular(header_receipt)
        receipt = json.loads(receipt_data)
        require(receipt["schema"] == "songplus-recovery-readiness-receipt/v1"
                and receipt["phase"] == "gpt-header"
                and receipt["status"] == "COLLECTED_PASSIVE_ONLY"
                and receipt["valid_payload"] is True and receipt["source_unchanged"] is True
                and receipt["exit_code"] == 0 and receipt["stdout_sha256"] == sha(data), "new header receipt mismatch")
        require(receipt["collector"] == "hardware/recovery-readiness/gpt_header_collector.py", "unexpected header collector")
        source = bounded_regular(ROOT / receipt["collector"])
        require(sha(source) == receipt["collector_sha256"], "new header collector source changed")
        for field in ("privilege_escalated", "target_writes_requested", "services_controlled", "usb_spi_control_io", "can_tx", "target_stack_started", "profile_binding_changed"):
            require(receipt[field] is False, "unexpected action in new receipt: " + field)
        require(current["schema"] == "songplus-recovery-gpt-header-passive/v1", "header evidence schema")
        require(current["disk_bytes"] == disk_bytes and current["sector_bytes"] == sector, "new geometry differs")
        headers = {}
        for label, name in (("primary", "sde-primary.bin"), ("secondary", "sde-physical_end.bin")):
            record = current["sde"][label]
            validate_collection_scope(record, label, disk_bytes)
            require(record["entries_sha256"] == sha(table), "new entries differ from 10/19 reconstruction")
            headers[label] = reconstruct_header(old[name], record, disk_bytes)
            require(describe_table(record, table) == {k: record[k] for k in describe_table(record, table)}, "new table CRC mismatch")
        require(current["sde"]["primary"]["entries_crc_valid"] is True and current["sde"]["secondary"]["entries_crc_valid"] is False, "new CRC condition differs")
        new_pair = validate_header_pair(current["sde"]["primary"], current["sde"]["secondary"], disk_bytes)
        validate_layout(table, current["sde"]["primary"], recovery["primary"]["partitions"])
        result["sources"].append({"path": str(Path(header_evidence).resolve().relative_to(ROOT)), "bytes": len(data), "sha256": sha(data), "kind": "new_readonly_header_evidence"})
        result["sources"].append({"path": str(Path(header_receipt).resolve().relative_to(ROOT)), "bytes": len(receipt_data), "sha256": sha(receipt_data), "kind": "new_readonly_header_receipt"})
        result["sources"].append({"path": receipt["collector"], "bytes": len(source), "sha256": sha(source), "kind": "new_readonly_header_collector"})
        result["current_header_validation"] = {
            "status": "EXACT_FULL_SECTOR_HASH_MATCH", "collected_utc": current["collected_utc"],
            "current_header_bytes_reconstructed": True, "current_crc_only_validation_performed": True,
            "both_headers_stable_across_individual_table_reads": True,
            "entry_hash_same_as_20261019": True, "header_pair_geometry": new_pair,
            "primary_header_sector_sha256": sha(headers["primary"]),
            "secondary_header_sector_sha256": sha(headers["secondary"]),
            "backup_crc_only_validation": crc_only_summary(headers["secondary"], table, sector, disk_bytes, disk_bytes // sector - 1),
            "payload_saved": False, "repair_candidate_deployable": False}
        # Preserve the first capture and its collector as audit history; its
        # weaker pre-read bounds are not accepted as the final collector.
        prior_files = (("gpt-headers.json", "prior_header_sample"),
                       ("gpt-headers-receipt.json", "prior_header_receipt"),
                       ("gpt-header-collector-original.txt", "prior_collector_snapshot"))
        prior_data = {}
        for filename, kind in prior_files:
            path = Path(header_evidence).parent / filename
            raw = bounded_regular(path)
            prior_data[filename] = raw
            result["sources"].append({"path": str(path.resolve().relative_to(ROOT)), "bytes": len(raw), "sha256": sha(raw), "kind": kind})
        prior = json.loads(prior_data["gpt-headers.json"])
        prior_receipt = json.loads(prior_data["gpt-headers-receipt.json"])
        require(prior_receipt["stdout_sha256"] == sha(prior_data["gpt-headers.json"])
                and prior_receipt["collector_sha256"] == sha(prior_data["gpt-header-collector-original.txt"]), "prior capture provenance mismatch")
        require(prior["sde"] == current["sde"], "first and fixed-scope header samples differ")
        result["prior_header_capture_review"] = {
            "collected_utc": prior["collected_utc"],
            "defect": "First collector bounded table reads to the whole disk, not the approved metadata regions.",
            "observed_reads_within_approved_metadata_regions": True,
            "all_observed_sde_fields_equal_fixed_scope_recapture": True,
            "original_evidence_and_collector_preserved": True,
            "final_analysis_uses_fixed_scope_recapture": True}
    for name in ("gpt_offline_analysis.py", "test_gpt_offline_analysis.py"):
        path = Path(__file__).parent / name
        data = bounded_regular(path)
        result["sources"].append({"path": str(path.relative_to(ROOT)), "bytes": len(data), "sha256": sha(data), "kind": "offline_analysis_source"})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header-evidence", type=Path, help="local redacted header evidence JSON; never a device")
    parser.add_argument("--header-receipt", type=Path, help="receipt from the separately authorized read-only sampler")
    args = parser.parse_args()
    print(json.dumps(analyze(args.header_evidence, args.header_receipt), indent=2, sort_keys=True))
