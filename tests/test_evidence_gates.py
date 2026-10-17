"""Evidence inventory regression tests; fixtures here are synthetic test data only."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


LAB = Path(__file__).resolve().parents[1]
SPEC = LAB / "spec"
module_spec = importlib.util.spec_from_file_location("songplus_evidence_gate", SPEC / "evidence_gate.py")
assert module_spec and module_spec.loader
gate = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(gate)


class TestEvidenceGates(unittest.TestCase):
    def setUp(self):
        self.protocol = json.loads((SPEC / "protocol-evidence.json").read_text())
        self.requirements = json.loads((SPEC / "safety-requirements.json").read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def result(self):
        return gate.validate_documents(self.protocol, self.requirements, self.base)

    def assertBlocked(self, code):
        result = self.result()
        self.assertFalse(result["schema_valid"], result)
        self.assertFalse(result["actuation_allowed"])
        self.assertTrue(any(error.startswith(code + ":") for error in result["errors"]), result)
        return result

    def write_fixture(self, name, body):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {"path": name, "sha256": hashlib.sha256(body).hexdigest()}

    def add_reviewed_test_evidence(self, claim):
        """Exercise structure with fake bytes in a temp directory, not real CAN proof."""
        family = claim["family"]
        rec = {"id": "test-recording", "vehicle_id": gate.TARGET_ID,
               "origin": "passive_vehicle_capture", "synthetic": False,
               "capture_session": "unit-test-only",
               **self.write_fixture("fixtures/recording.bin", b"UNIT TEST ONLY: placeholder bytes\n")}
        trace = {"id": "test-trace", "vehicle_id": gate.TARGET_ID, "family": family,
                 "recording_id": rec["id"], "claim_ids": [claim["id"]], "requirement_ids": []}
        manifest = {key: trace[key] for key in ("vehicle_id", "family", "recording_id", "claim_ids", "requirement_ids")}
        manifest.update(recording_sha256=rec["sha256"],
                        observations=[{"start_frame": 0, "end_frame": 1, "finding": "unit test only"}],
                        review={"status": "reviewed", "reviewer": "unit-test-fixture"})
        trace.update(self.write_fixture("fixtures/trace.json", json.dumps(manifest).encode()))
        self.protocol["evidence"]["recordings"].append(rec)
        self.protocol["evidence"]["traces"].append(trace)
        self.protocol["target_hypothesis"]["selected_family"] = family
        claim.update(status="verified", vehicle_compatibility="verified_from_passive_evidence",
                     target_vehicle_interpretation={"note": "unit test only"},
                     recording_refs=[rec["id"]], trace_refs=[trace["id"]])
        self.protocol["vehicle_confirmed_claim_count"] = 1
        return rec, trace

    def test_repository_starts_with_zero_vehicle_confirmations(self):
        self.assertEqual(20, len(self.protocol["claims"]))
        self.assertTrue(all(claim["status"] in ("candidate", "unknown") for claim in self.protocol["claims"]))
        self.assertEqual({"recordings": [], "traces": [], "artifacts": []}, self.protocol["evidence"])
        result = self.result()
        self.assertTrue(result["offline_research_ready"], result)
        self.assertEqual(0, result["vehicle_confirmed_claim_count"])
        self.assertFalse(result["actuation_allowed"])
        self.assertFalse(result["firmware_generated"])
        self.assertFalse(result["can_messages_generated"])

    def test_exact_variant_required(self):
        self.protocol["target_vehicle"]["trim"] = "Flagship"
        self.assertBlocked("target_identity")

    def test_forged_confirmation_count(self):
        self.protocol["vehicle_confirmed_claim_count"] = 20
        self.assertBlocked("confirmed_count_mismatch")

    def test_verified_requires_recording_and_trace(self):
        claim = self.protocol["claims"][0]
        claim["status"] = "verified"
        self.protocol["vehicle_confirmed_claim_count"] = 1
        result = self.assertBlocked("verified_requires_recording")
        self.assertTrue(any(error.startswith("verified_requires_trace:") for error in result["errors"]))

    def test_status_cannot_say_complete(self):
        self.protocol["claims"][0]["status"] = "complete"
        self.assertBlocked("claim_status")

    def test_same_name_is_not_compatibility(self):
        self.protocol["target_hypothesis"]["compatibility"] = "verified_from_passive_evidence"
        self.protocol["target_hypothesis"]["selected_family"] = "huheasHanTorque"
        self.protocol["target_hypothesis"]["basis"] = "BYD_SONG_PLUS_DMI_23 exists in values.py"
        self.assertBlocked("compatibility_requires_evidence")

    def test_candidate_cannot_assert_vehicle_compatibility(self):
        self.protocol["claims"][0]["vehicle_compatibility"] = "verified_from_passive_evidence"
        self.assertBlocked("candidate_is_not_compatibility")

    def test_pinned_source_is_required(self):
        self.protocol["sources"].pop()
        self.assertBlocked("source_set")

    def test_claim_cannot_drop_source(self):
        self.protocol["claims"][0]["source_urls"] = []
        self.assertBlocked("claim_source_missing")

    def test_mutable_branch_url_rejected(self):
        source = self.protocol["sources"][0]
        source["documents"][0] = source["documents"][0].replace(source["commit"], "master")
        self.assertBlocked("source_url")

    def test_source_url_from_other_family_rejected(self):
        self.protocol["claims"][0]["source_urls"] = self.protocol["sources"][1]["documents"][:1]
        self.assertBlocked("claim_source_missing")

    def test_all_execution_switches_fail_closed(self):
        for flag in ("actuation_enabled", "can_transmit", "firmware_generation"):
            with self.subTest(flag=flag):
                self.protocol["execution"][flag] = True
                self.assertBlocked("actuation_forbidden")
                self.protocol["execution"][flag] = False

    def test_requirements_cannot_authorize_actuation(self):
        self.requirements["actuation_allowed"] = True
        self.assertBlocked("actuation_forbidden")

    def test_cached_safety_parameter_cannot_be_inherited(self):
        self.protocol["safety_parameters"]["inherits_existing_parameters"] = True
        self.protocol["safety_parameters"]["proposed_safety_param"] = 33
        self.assertBlocked("safety_parameter_unassigned")

    def test_legacy_observation_cannot_be_marked_reusable(self):
        self.protocol["safety_parameters"]["legacy_observation"]["may_reuse"] = True
        self.assertBlocked("legacy_parameter_contract")

    def test_longitudinal_stage_blocked_without_lateral_acc_evidence(self):
        self.protocol["requested_stage"] = "longitudinal_offline"
        result = self.result()
        self.assertTrue(result["schema_valid"], result)
        self.assertFalse(result["stage_ready"])
        for requirement in ("stock_acc_isolation", "stock_aeb_preservation", "lateral_boundary_and_fault_tests"):
            self.assertIn(requirement, result["blocked_stage_gates"])

    def test_forged_completed_requirements_need_fixtures(self):
        for requirement in self.requirements["requirements"]:
            requirement["status"] = "evidenced"
        self.protocol["requested_stage"] = "longitudinal_offline"
        self.assertBlocked("requirement_requires_fixture")

    def test_cannot_remove_required_acc_isolation_gate(self):
        self.requirements["requirements"] = [x for x in self.requirements["requirements"] if x["id"] != "stock_acc_isolation"]
        self.assertBlocked("required_gate_set")

    def test_missing_recording_fixture(self):
        rec, _ = self.add_reviewed_test_evidence(self.protocol["claims"][0])
        (self.base / rec["path"]).unlink()
        self.assertBlocked("fixture_missing_or_empty")

    def test_recording_hash_tamper(self):
        rec, _ = self.add_reviewed_test_evidence(self.protocol["claims"][0])
        (self.base / rec["path"]).write_bytes(b"different bytes")
        self.assertBlocked("fixture_hash_mismatch")

    def test_synthetic_capture_cannot_confirm_vehicle(self):
        rec, _ = self.add_reviewed_test_evidence(self.protocol["claims"][0])
        rec["synthetic"] = True
        self.assertBlocked("recording_provenance")

    def test_recording_from_other_variant_rejected(self):
        rec, _ = self.add_reviewed_test_evidence(self.protocol["claims"][0])
        rec["vehicle_id"] = "BYD_SONG_PLUS_DMI_23_ALT"
        self.assertBlocked("recording_provenance")

    def test_empty_trace_is_not_evidence(self):
        _, trace = self.add_reviewed_test_evidence(self.protocol["claims"][0])
        trace.update(self.write_fixture(trace["path"], b"{}"))
        self.assertBlocked("trace_manifest_binding")

    def test_trace_cannot_reference_other_recording(self):
        _, trace = self.add_reviewed_test_evidence(self.protocol["claims"][0])
        trace["recording_id"] = "other-recording"
        self.assertBlocked("trace_binding")

    def test_no_cross_family_verified_claims(self):
        claim = self.protocol["claims"][0]
        self.add_reviewed_test_evidence(claim)
        self.protocol["target_hypothesis"]["selected_family"] = "huheasHanTorque"
        self.assertBlocked("mixed_protocol_families")

    def test_inventory_integrity_pass_still_never_authorizes_actuation(self):
        self.add_reviewed_test_evidence(self.protocol["claims"][0])
        result = self.result()
        self.assertTrue(result["schema_valid"], result)
        self.assertFalse(result["actuation_allowed"])
        self.assertIn("no certification", result["meaning"])
        # This test intentionally demonstrates the validator's limited claim:
        # it verifies references and hashes, not whether bytes mean steering.

    def test_fixture_escape_rejected(self):
        rec, _ = self.add_reviewed_test_evidence(self.protocol["claims"][0])
        rec["path"] = "../outside.bin"
        self.assertBlocked("fixture_escape")

    def test_malformed_shapes_fail_closed(self):
        for mutation in (lambda p: p.update(evidence=[]),
                         lambda p: p.update(target_hypothesis=[]),
                         lambda p: p["claims"][0].update(family=[])):
            with self.subTest(mutation=mutation):
                protocol = copy.deepcopy(self.protocol)
                mutation(protocol)
                result = gate.validate_documents(protocol, self.requirements, self.base)
                self.assertFalse(result["schema_valid"], result)
                self.assertFalse(result["actuation_allowed"])

    def test_cli_reports_research_state_and_no_actuation(self):
        result = subprocess.run([sys.executable, str(SPEC / "evidence_gate.py")],
                                text=True, capture_output=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(0, report["vehicle_confirmed_claim_count"])
        self.assertFalse(report["actuation_allowed"])


if __name__ == "__main__":
    unittest.main()
