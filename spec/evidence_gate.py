"""Validate offline research evidence; never author firmware or control messages.

Passing this validator means the evidence inventory is internally consistent.
It does not establish that a CAN interpretation or safety implementation is correct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


TARGET_ID = "byd_song_plus_dmi_2023_champion_110km_flagship_plus"
TARGET = {
    "id": TARGET_ID,
    "make": "BYD",
    "model": "Song Plus DM-i",
    "model_year": 2023,
    "edition": "Champion",
    "electric_range_km": 110,
    "trim": "Flagship PLUS",
}
SOURCES = {
    "officialAtto3": ("commaai/opendbc", "06ade56b49162e3f35e354df115e97b02de81d7a"),
    "huheasHanTorque": ("huheas/carrotpilot", "43a030a5fe76c44697431bd75b20e82792ec611e"),
    "qzwfAtto3": ("qzwf/opendbc", "591705c9dd1c527d9e732d41bc2c80e468654cbb"),
}
REQUIRED_GATES = {
    "target_protocol_identity": "trace",
    "steering_command_and_feedback": "trace",
    "driver_brake_cancel_override": "trace",
    "rx_integrity_and_liveness": "trace",
    "tx_allowlist_and_disengaged_state": "artifact",
    "relay_and_forward_ownership": "trace",
    "stock_acc_isolation": "trace",
    "stock_aeb_preservation": "trace",
    "lateral_boundary_and_fault_tests": "artifact",
    "build_registration_and_release_tests": "artifact",
    "independent_safety_review": "artifact",
    "longitudinal_command_and_neutral": "trace",
    "longitudinal_pedal_override": "trace",
    "longitudinal_limits_and_aeb_tests": "artifact",
}
LONGITUDINAL_PREREQUISITES = (
    "target_protocol_identity",
    "steering_command_and_feedback",
    "driver_brake_cancel_override",
    "rx_integrity_and_liveness",
    "tx_allowlist_and_disengaged_state",
    "relay_and_forward_ownership",
    "stock_acc_isolation",
    "stock_aeb_preservation",
    "lateral_boundary_and_fault_tests",
)
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


def _validate_documents(protocol: dict[str, Any], requirements: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Check inventory integrity and development-stage gates without transmitting.

    Local fixtures are resolved inside base_dir and hash checked. Claims promoted to
    verified need a non-synthetic passive capture and a trace manifest that binds
    the exact vehicle, source family, recording hash, and claim. Human trace review
    is still necessary; this is a provenance check, not an interpretation oracle.
    """
    errors: list[str] = []
    base_dir = Path(base_dir).resolve()

    def error(code: str, detail: str) -> None:
        errors.append(f"{code}: {detail}")

    def index(items: Any, label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not isinstance(items, list):
            error("invalid_collection", label)
            return result
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                error("invalid_id", label)
                continue
            if item["id"] in result:
                error("duplicate_id", f"{label}/{item['id']}")
            result[item["id"]] = item
        return result

    def fixture(item: dict[str, Any], label: str) -> Path | None:
        rel = item.get("path")
        digest = item.get("sha256")
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
            error("fixture_path", label)
            return None
        resolved = (base_dir / rel).resolve()
        if not resolved.is_relative_to(base_dir):
            error("fixture_escape", label)
            return None
        if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
            error("fixture_hash_missing", label)
            return None
        if not resolved.is_file() or resolved.stat().st_size == 0:
            error("fixture_missing_or_empty", label)
            return None
        h = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            error("fixture_hash_mismatch", label)
            return None
        return resolved

    if not isinstance(protocol, dict) or not isinstance(requirements, dict):
        return {"schema_valid": False, "offline_research_ready": False,
                "actuation_allowed": False, "errors": ["invalid_document: expected two objects"]}

    if protocol.get("schema_version") != 1 or requirements.get("schema_version") != 1:
        error("schema_version", "both documents must use schema version 1")
    if protocol.get("target_vehicle") != TARGET or requirements.get("target_vehicle_id") != TARGET_ID:
        error("target_identity", "the exact Champion 110km Flagship PLUS identity is required")
    if protocol.get("scope") != "offline_research" or requirements.get("scope") != "offline_research":
        error("scope", "this laboratory supports offline research only")

    execution = protocol.get("execution", {})
    if not isinstance(execution, dict) or any(execution.get(key) is not False for key in
                                             ("actuation_enabled", "can_transmit", "firmware_generation")):
        error("actuation_forbidden", "all execution capabilities must explicitly remain false")
    if requirements.get("actuation_allowed") is not False:
        error("actuation_forbidden", "requirements cannot authorize actuation")
    params = protocol.get("safety_parameters", {})
    if not isinstance(params, dict) or params.get("inherits_existing_parameters") is not False or params.get("proposed_safety_param") is not None:
        error("safety_parameter_unassigned", "no inherited or proposed safetyParam is allowed at this stage")
    if isinstance(params, dict) and params.get("legacy_observation") != {"value": 33, "meaning": "unresolved_fork_specific", "may_reuse": False}:
        error("legacy_parameter_contract", "33 is only an unresolved observation, never a reusable parameter")

    sources = index(protocol.get("sources"), "sources")
    if set(sources) != set(SOURCES):
        error("source_set", "retain the three distinct, pinned source families")
    for family, source in sources.items():
        expected = SOURCES.get(family)
        if expected is None:
            error("unknown_family", family)
            continue
        repo, commit = expected
        prefix = f"https://github.com/{repo}/blob/{commit}/"
        if source.get("repository") != repo or source.get("commit") != commit:
            error("source_pin", family)
        if source.get("family") != family or source.get("target_vehicle_compatibility") != "unverified":
            error("candidate_is_not_compatibility", family)
        documents = source.get("documents")
        if not isinstance(documents, list) or not documents:
            error("source_documents", family)
        elif any(not isinstance(url, str) or not url.startswith(prefix) or not re.search(r"#L\d+(?:-L\d+)?$", url) for url in documents):
            error("source_url", family)

    evidence = protocol.get("evidence", {})
    if not isinstance(evidence, dict):
        error("evidence_collection", "evidence must be an object")
        evidence = {}
    recordings = index(evidence.get("recordings"), "recordings")
    traces = index(evidence.get("traces"), "traces")
    artifacts = index(evidence.get("artifacts"), "artifacts")
    if len(set(recordings) | set(traces) | set(artifacts)) != len(recordings) + len(traces) + len(artifacts):
        error("evidence_id_collision", "evidence IDs must be globally unique")
    valid_recordings: set[str] = set()
    valid_traces: set[str] = set()
    valid_artifacts: set[str] = set()
    for rec_id, rec in recordings.items():
        before = len(errors)
        if rec.get("vehicle_id") != TARGET_ID or rec.get("origin") != "passive_vehicle_capture" or rec.get("synthetic") is not False:
            error("recording_provenance", rec_id)
        if not isinstance(rec.get("capture_session"), str) or not rec.get("capture_session"):
            error("capture_session", rec_id)
        fixture(rec, rec_id)
        if len(errors) == before:
            valid_recordings.add(rec_id)

    for trace_id, trace in traces.items():
        before = len(errors)
        recording_id = trace.get("recording_id")
        family = trace.get("family")
        for field in ("claim_ids", "requirement_ids"):
            values = trace.get(field)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                error("trace_reference_list", f"{trace_id}/{field}")
        if recording_id not in valid_recordings or family not in SOURCES or trace.get("vehicle_id") != TARGET_ID:
            error("trace_binding", trace_id)
        path = fixture(trace, trace_id)
        manifest: Any = None
        if path:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                error("trace_manifest", trace_id)
        if isinstance(manifest, dict):
            for key in ("recording_id", "family", "vehicle_id", "claim_ids", "requirement_ids"):
                if manifest.get(key) != trace.get(key):
                    error("trace_manifest_binding", f"{trace_id}/{key}")
            rec = recordings.get(recording_id, {})
            if manifest.get("recording_sha256") != rec.get("sha256"):
                error("trace_recording_hash", trace_id)
            observations = manifest.get("observations")
            if not isinstance(observations, list) or not observations:
                error("trace_observations", trace_id)
            else:
                for obs in observations:
                    if (not isinstance(obs, dict) or type(obs.get("start_frame")) is not int or
                        type(obs.get("end_frame")) is not int or obs["start_frame"] < 0 or
                        obs["end_frame"] < obs["start_frame"] or not obs.get("finding")):
                        error("trace_observation_range", trace_id)
            if manifest.get("review", {}).get("status") != "reviewed" or not manifest.get("review", {}).get("reviewer"):
                error("trace_review_missing", trace_id)
        elif path:
            error("trace_manifest", trace_id)
        if len(errors) == before:
            valid_traces.add(trace_id)

    for artifact_id, artifact in artifacts.items():
        before = len(errors)
        if artifact.get("kind") not in ("offline_test_result", "source_review") or artifact.get("vehicle_id") != TARGET_ID:
            error("artifact_provenance", artifact_id)
        if artifact.get("family") not in SOURCES or not artifact.get("requirement_ids"):
            error("artifact_binding", artifact_id)
        fixture(artifact, artifact_id)
        if len(errors) == before:
            valid_artifacts.add(artifact_id)

    hypothesis = protocol.get("target_hypothesis", {})
    if not isinstance(hypothesis, dict):
        error("hypothesis", "expected object")
        hypothesis = {}
    selected_family = hypothesis.get("selected_family")
    if selected_family is not None and selected_family not in SOURCES:
        error("unknown_family", str(selected_family))
    compatibility = hypothesis.get("compatibility")
    if compatibility not in ("unknown", "candidate", "verified_from_passive_evidence"):
        error("candidate_is_not_compatibility", str(compatibility))

    claims = index(protocol.get("claims"), "claims")
    if not claims:
        error("missing_claim_inventory", "retain the protocol candidate inventory")
    verified: list[str] = []
    for claim_id, claim in claims.items():
        family = claim.get("family")
        status = claim.get("status")
        if family not in SOURCES:
            error("unknown_family", claim_id)
        urls = claim.get("source_urls")
        allowed_urls = sources.get(family, {}).get("documents", [])
        if not isinstance(urls, list) or not urls or any(url not in allowed_urls for url in urls):
            error("claim_source_missing", claim_id)
        if status not in ("unknown", "candidate", "verified"):
            error("claim_status", claim_id)
        can = claim.get("candidate_can")
        if (not isinstance(can, dict) or not isinstance(can.get("address"), str) or
            not re.fullmatch(r"0x[0-9A-F]{1,8}", can["address"]) or
            type(can.get("bus")) is not int or can["bus"] not in (0, 1, 2) or
            type(can.get("dlc")) is not int or not 1 <= can["dlc"] <= 64):
            error("can_candidate_shape", claim_id)
        if claim.get("vehicle_compatibility") not in ("unverified", "verified_from_passive_evidence"):
            error("candidate_is_not_compatibility", claim_id)
        if status != "verified" and claim.get("vehicle_compatibility") != "unverified":
            error("candidate_is_not_compatibility", claim_id)
        if status == "verified":
            verified.append(claim_id)
            if claim.get("vehicle_compatibility") != "verified_from_passive_evidence" or not claim.get("target_vehicle_interpretation"):
                error("verified_target_interpretation", claim_id)
            if family != selected_family:
                error("mixed_protocol_families", claim_id)
            rec_ids, trace_ids = claim.get("recording_refs"), claim.get("trace_refs")
            if not isinstance(rec_ids, list) or not rec_ids or any(ref not in valid_recordings for ref in rec_ids):
                error("verified_requires_recording", claim_id)
            if not isinstance(trace_ids, list) or not trace_ids or any(ref not in valid_traces for ref in trace_ids):
                error("verified_requires_trace", claim_id)
            else:
                for ref in trace_ids:
                    trace = traces[ref]
                    if trace.get("family") != family or claim_id not in trace.get("claim_ids", []) or trace.get("recording_id") not in (rec_ids or []):
                        error("claim_trace_binding", claim_id)
    actual_count = len(verified)
    if type(protocol.get("vehicle_confirmed_claim_count")) is not int or protocol["vehicle_confirmed_claim_count"] != actual_count:
        error("confirmed_count_mismatch", f"computed {actual_count}")
    if compatibility == "verified_from_passive_evidence" and (not verified or selected_family is None):
        error("compatibility_requires_evidence", "a matching name or enum cannot confirm compatibility")
    for trace_id, trace in traces.items():
        if any(ref not in claims for ref in trace.get("claim_ids", [])):
            error("trace_unknown_claim", trace_id)
        if any(ref not in REQUIRED_GATES for ref in trace.get("requirement_ids", [])):
            error("trace_unknown_requirement", trace_id)

    gates = index(requirements.get("requirements"), "requirements")
    if set(gates) != set(REQUIRED_GATES):
        error("required_gate_set", "required safety evidence gates may not be removed or renamed")
    evidenced_gates: set[str] = set()
    for gate_id, gate in gates.items():
        if gate.get("required_evidence_kind") != REQUIRED_GATES.get(gate_id):
            error("requirement_kind", gate_id)
        if gate.get("status") not in ("unknown", "blocked", "evidenced"):
            error("requirement_status", gate_id)
        refs = gate.get("evidence_refs")
        if not isinstance(refs, list):
            error("requirement_evidence_refs", gate_id)
            continue
        if gate.get("status") == "evidenced":
            before = len(errors)
            expected_kind = REQUIRED_GATES.get(gate_id)
            valid_ids = valid_traces if expected_kind == "trace" else valid_artifacts
            records = traces if expected_kind == "trace" else artifacts
            if not refs or any(ref not in valid_ids for ref in refs):
                error("requirement_requires_fixture", gate_id)
            for ref in refs:
                item = records.get(ref, {})
                if gate_id not in item.get("requirement_ids", []) or selected_family is None or item.get("family") != selected_family:
                    error("requirement_fixture_binding", gate_id)
            if len(errors) == before:
                evidenced_gates.add(gate_id)

    stage = protocol.get("requested_stage")
    if stage not in ("protocol_research", "lateral_offline", "longitudinal_offline"):
        error("stage", "only declared offline development stages are supported")
    blocked_gates = [gate for gate in LONGITUDINAL_PREREQUISITES if gate not in evidenced_gates] if stage == "longitudinal_offline" else []
    return {
        "schema_valid": not errors,
        "offline_research_ready": not errors and not blocked_gates,
        "requested_stage": stage,
        "stage_ready": not errors and not blocked_gates,
        "actuation_allowed": False,
        "firmware_generated": False,
        "can_messages_generated": False,
        "vehicle_confirmed_claim_count": actual_count if not errors else 0,
        "declared_verified_claim_count": actual_count,
        "unresolved_requirements": sorted(set(REQUIRED_GATES) - evidenced_gates),
        "blocked_stage_gates": blocked_gates,
        "errors": errors,
        "meaning": "inventory consistency only; no certification of CAN semantics or vehicle safety",
    }


def validate_documents(protocol: dict[str, Any], requirements: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Fail closed on malformed JSON shapes and inaccessible local evidence."""
    try:
        return _validate_documents(protocol, requirements, base_dir)
    except (TypeError, AttributeError, KeyError, ValueError, OSError) as exc:
        return {
            "schema_valid": False,
            "offline_research_ready": False,
            "stage_ready": False,
            "actuation_allowed": False,
            "firmware_generated": False,
            "can_messages_generated": False,
            "errors": [f"invalid_evidence_structure: {type(exc).__name__}: {exc}"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=Path(__file__).with_name("protocol-evidence.json"))
    parser.add_argument("--requirements", type=Path, default=Path(__file__).with_name("safety-requirements.json"))
    parser.add_argument("--base-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
        requirements = json.loads(args.requirements.read_text(encoding="utf-8"))
        result = validate_documents(protocol, requirements, args.base_dir)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"schema_valid": False, "offline_research_ready": False, "actuation_allowed": False,
                  "errors": [f"document_read: {exc}"]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["offline_research_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
