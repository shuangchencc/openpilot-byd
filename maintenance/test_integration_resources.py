"""Source-only negative tests for retained resource evidence and signing trust.

Fixtures contain synthetic text, not ARM code. Their fixed public-development
RSA signature exercises real byte verification without compiler/private inputs.
Command transcript validation is covered separately and mocked here so these
tests focus on independently reconstructed source, artifact and audit contracts.
"""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from maintenance import integration_evidence as stage
from maintenance.child_evidence import SIGNATURE_LABEL

PUBLIC_KEY = ("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQC948lnRo4x44Rd7Y8bQAML4aKDC4XRx958fHV8K6+FbCaP1Z42U2kX0yygak0LjoDutpgObmGHZA+Iz3HeUD6VGjr/teN24vPk+A95cRsjt8rgmGQ96HNjaNgjR+gl1F9XxFimMzir82Xpl1ekTueJNXa7ia5HVH1nFdiksOKHGQ== batman@y840\n")
RAW = base64.b64decode("AAAAAHN5bnRoZXRpYyBQOCBjb250cmFjdCB0ZXN0OyBub3QgQVJNIGNvZGU=")
SIGNED = base64.b64decode("NAAAAHN5bnRoZXRpYyBQOCBjb250cmFjdCB0ZXN0OyBub3QgQVJNIGNvZGVWRVJTAgAAAFLMS3g9dyfCNxHzHhaAkovNmNas1BrVlO6Bl7lJ5KzcF5C5o62Oe6+gpGJyXiB8peiV72lDA6Qe3ZTNgDqyx0MlTF9+xnMy0Ha2+69jPH6HsNGTuHpcF+vHYPndahujUPTHjU4vLuAe5SeS0nJTxPRJdx0fM3kUTA84GKYVUMB9")


def digest(data):
  return hashlib.sha256(data).hexdigest()


def write(path, data):
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(data))


class ResourceEvidenceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name).resolve()
    self.base = self.root / "result"
    board = {"board/main.c": b"synthetic main", "board/health.h": b"synthetic health",
             "board/crypto/certs/debug.pub": PUBLIC_KEY.encode()}
    safety = {"opendbc/safety/can.h": b"synthetic safety"}
    board_hashes = {name: digest(data) for name, data in board.items()}
    safety_hashes = {name: digest(data) for name, data in safety.items()}
    identity = digest(json.dumps({"board": board_hashes, "safety": safety_hashes}, sort_keys=True).encode())
    parent_path = self.root / "patches/panda/p5/manifest.json"
    write(parent_path, {"baseline_inputs": {"upstream-openpilot/opendbc_repo/" + name: checksum for name, checksum in safety_hashes.items()}})
    manifest = {"parent_manifest_sha256": stage.sha(parent_path), "result_tree": board_hashes,
                "baselines": {"fixture": "not a hardware baseline"}, "series": [{"commit": "a" * 40}]}
    manifest_path = self.root / "patches/panda/p8/manifest.json"
    write(manifest_path, manifest)
    self.report = {"schema": "songplus-p8-resources-offline/v1", "status": "PASS_OFFLINE_CANDIDATE",
                   "manifest_sha256": stage.sha(manifest_path), "parent_manifest_sha256": manifest["parent_manifest_sha256"],
                   "baselines": manifest["baselines"], "source_commit": "a" * 40,
                   **dict.fromkeys(("device_access", "mcu_executed", "bootstub_built", "deployable"), False),
                   "runtime_resource_gate": "BLOCKED", "summary": {"runtime_resource_proof_passed": False},
                   "commands": [], "reports": {}, "reproducibility": []}
    for rep in ("first", "second"):
      panda, dbc = self.base / rep / "panda", self.base / rep / "opendbc"
      for base, content in ((panda, board), (dbc, safety)):
        for name, data in content.items():
          p = base / name
          p.parent.mkdir(parents=True, exist_ok=True)
          p.write_bytes(data)
      for variant in stage.VARIANTS:
        target, policy = variant.split("-")
        folder = self.base / rep / variant
        folder.mkdir(parents=True)
        artifact_data = dict.fromkeys(("build.log", "main.ci", "main.d", "main.elf", "main.map", "main.o", "main.su", "startup.o"), b"synthetic resource fixture")
        artifact_data.update({"main.bin": RAW, "main.bin.signed": SIGNED})
        for name, data in artifact_data.items():
          (folder / name).write_bytes(data)
        artifacts = {name: digest(data) for name, data in artifact_data.items()}
        family = "stm32f4" if target == "f4" else "stm32h7"
        startup = "startup_stm32f413xx.s" if target == "f4" else "startup_stm32h7x5xx.s"
        linker = "stm32f4_flash.ld" if target == "f4" else "stm32h7_app.ld"
        cc = ["fixture-gcc", *(["-DALLOW_DEBUG"] if policy == "debug" else [])]
        build = {"schema": "songplus-p8-app-build/v1", "target": target, "policy": policy,
                 "deployable": False, "bootstub_built": False, "board_inputs": board_hashes,
                 "safety_inputs": safety_hashes, "source_identity": identity,
                 "protocol_inputs": {"board/health.h": board_hashes["board/health.h"], "opendbc/safety/can.h": safety_hashes["opendbc/safety/can.h"]},
                 "version": f"P8-{target}-{identity[:12]}-{policy}", "signature_key": SIGNATURE_LABEL, "artifacts": artifacts,
                 "commands": [["fixture-gcc", "--version"],
                              [*cc, "-fstack-usage", "-fcallgraph-info=su", "-c", str(panda / "board/main.c"), "-o", str(folder / "main.o")],
                              [*cc, "-c", str(panda / "board" / family / startup), "-o", str(folder / "startup.o")],
                              [*cc, "-T" + str(panda / "board" / family / linker), str(folder / "startup.o"), str(folder / "main.o"), "-o", str(folder / "main.elf")],
                              ["fixture-objcopy", "-O", "binary", str(folder / "main.elf"), str(folder / "main.bin")],
                              ["fixture-python", str(panda / "board/crypto/sign.py"), str(folder / "main.bin"), str(folder / "main.bin.signed"), str(panda / "board/crypto/certs/debug")]]}
        frame = {"bytes": 8, "qualifier": "static", "source": str(folder / "main.su")}
        audit = {"status": "PASS", "target": target, "static_allocation_passed": True, "device_access": False,
                 "boot_cookies": {"symbols": {name: {"start": address, "end_exclusive": address + 4, "bytes": 4, "overlapping_objects": []}
                  for name, address in {"enter_bootloader_mode": 0x2001FFFC if target == "f4" else 0x38001FFC,
                                        "fault_cookie": 0x2001FFF8 if target == "f4" else 0x38003FFC}.items()},
                                  "isolated_from_live_objects": True, "runtime_impact_validated": False, "inherited_h7_overlap_removed": target == "h7"},
                 "stack": {"reserved_bytes": 8192 if target == "f4" else 1024, "worst_case_call_and_interrupt_depth_proven": False,
                           "frames": [frame], "maximum_single_frame": frame,
                           "direct_callgraph": {"complete_worst_case_bound": False, "cycles": [], "compiler_nodes": 1,
                                                "maximum_known_direct_chain_bytes": 8,
                                                "files": [{"path": str(folder / "main.ci"), "sha256": artifacts["main.ci"]}]}},
                 "runtime_resource_gate": {"status": "BLOCKED", "deployable": False, "reasons": ["fixture physical", "fixture stack", "fixture target"]},
                 "signature": {"scheme": "official panda development RSA1024/SHA1", "version": 2,
                               "app_length_with_version": len(RAW) + 8, "signature_bytes": 128,
                               "verified_with_public_key_sha256": board_hashes["board/crypto/certs/debug.pub"]},
                 "flash": {"raw_bytes": len(RAW), "signed_bytes": len(SIGNED)},
                 "inputs": [{"path": str(folder / name), "sha256": artifacts[name]} for name in
                            ("main.elf", "main.map", "main.bin", "main.bin.signed", "main.su", "main.ci")]
                           + [{"path": str(panda / "board/crypto/certs/debug.pub"), "sha256": board_hashes["board/crypto/certs/debug.pub"]}]}
        self.report["reports"][rep + "-" + variant] = {"build": build, "resources": audit}
        if rep == "first":
          for name in ("main.bin", "main.bin.signed", "main.elf"):
            self.report["reproducibility"].append({"artifact": variant + "/" + name, "byte_identical": True, "sha256": artifacts[name]})
    boundaries = ("f4-flash", "f4-static", "h7-flash", "h7-static-heap", "h7-sram4-rx", "h7-sram4-tx")
    checks = {name + suffix for name in boundaries for suffix in ("-exact", "-overflow")}
    checks.update(t + suffix for t in ("f4", "h7") for suffix in ("-frame-overflow", "-unbounded-frame", "-direct-chain-overflow", "-recursive-chain", "-runtime-proof"))
    checks.add("inherited-overlap-rejected")
    fixtures = {"resource-faults": {"passed": 23, "runtime_proof_completed": False, "runtime_resource_gate": "BLOCKED",
                                  "checks": [{"name": name, "passed": True} for name in sorted(checks)], "commands": []},
                "protocol": {"protocol": [{"groups": 15}, {"groups": 15}], "abi": [{"negative_compile_cases": [1, 2, 3]}] * 2},
                "bxcan": {"groups": 24}, "fdcan": {"groups": 14}, "bsp": {"summary": {"groups": 23}},
                "capabilities": {"tests_run": 12}, "safety": {"tests_run": 49, "selectors": list(range(49))},
                "liveness-f4": {"summary": {"passed": 13}}, "liveness-h7": {"summary": {"passed": 13}}}
    for name, data in fixtures.items():
      self.report["reports"][name] = {"status": "PASS", **data}
    self.flush()

  def flush(self):
    for rep in ("first", "second"):
      for variant in stage.VARIANTS:
        item = self.report["reports"][rep + "-" + variant]
        for source, filename in (("build", "build.json"), ("resources", "resources.json")):
          write(self.base / rep / variant / filename, item[source])
    for name, item in self.report["reports"].items():
      if name.startswith(("first-", "second-")):
        continue
      filename = "validation.json" if name == "protocol" else "safety.json" if name == "safety" else "result.json"
      write(self.base / name / filename, item)

  @property
  def build(self):
    return self.report["reports"]["first-f4-debug"]["build"]

  @property
  def audit(self):
    return self.report["reports"]["first-f4-debug"]["resources"]

  def validate(self):
    self.flush()
    with patch.object(stage, "check_commands"):
      return stage.resources(self.report, self.base, self.root)

  def test_complete_synthetic_resource_contract_and_real_public_signature(self):
    self.assertEqual(self.validate()["arm_builds"], 8)

  def test_empty_cookie_inventory(self):
    self.audit["boot_cookies"]["symbols"] = {}
    with self.assertRaisesRegex(ValueError, "cookie symbol inventory"):
      self.validate()

  def test_cookie_abi_drift(self):
    self.audit["boot_cookies"]["symbols"]["fault_cookie"]["start"] -= 4
    with self.assertRaisesRegex(ValueError, "Cookie ABI"):
      self.validate()

  def test_cookie_live_overlap(self):
    self.audit["boot_cookies"]["symbols"]["fault_cookie"]["overlapping_objects"] = ["mic"]
    with self.assertRaisesRegex(ValueError, "Cookie ABI"):
      self.validate()

  def test_empty_audit_inputs(self):
    self.audit["inputs"] = []
    with self.assertRaisesRegex(ValueError, "input inventory"):
      self.validate()

  def test_duplicate_audit_input(self):
    self.audit["inputs"][-1] = self.audit["inputs"][0]
    with self.assertRaisesRegex(ValueError, "input inventory"):
      self.validate()

  def test_audit_input_cannot_borrow_another_build(self):
    self.audit["inputs"][0]["path"] = str(self.base / "second/f4-debug/main.elf")
    with self.assertRaisesRegex(ValueError, "input inventory"):
      self.validate()

  def test_board_inventory_not_self_attested(self):
    self.build["board_inputs"] = {}
    with self.assertRaisesRegex(ValueError, "independently locked"):
      self.validate()

  def test_safety_source_not_self_attested(self):
    self.build["safety_inputs"] = {"opendbc/safety/can.h": "0" * 64}
    with self.assertRaisesRegex(ValueError, "independently locked"):
      self.validate()

  def test_source_identity_rederived(self):
    self.build["source_identity"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "independently locked"):
      self.validate()

  def test_retained_source_tree_rehashed(self):
    (self.base / "first/panda/board/main.c").write_text("changed")
    with self.assertRaisesRegex(ValueError, "Retained Panda source tree"):
      self.validate()

  def test_retained_official_safety_rehashed(self):
    (self.base / "first/opendbc/opendbc/safety/can.h").write_text("changed")
    with self.assertRaisesRegex(ValueError, "Retained safety header"):
      self.validate()

  def test_unknown_extra_artifact(self):
    self.build["artifacts"]["installer.bin"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "artifact inventory"):
      self.validate()

  def test_missing_compile_step(self):
    del self.build["commands"][1]
    with self.assertRaisesRegex(ValueError, "command inventory"):
      self.validate()

  def test_debug_policy_cannot_masquerade_as_release(self):
    self.build["commands"][1].remove("-DALLOW_DEBUG")
    with self.assertRaisesRegex(ValueError, "policy/stack"):
      self.validate()

  def test_production_trust_label_rejected(self):
    self.build["signature_key"] = "production key"
    with self.assertRaisesRegex(ValueError, "signing policy"):
      self.validate()

  def test_self_consistent_corrupt_signature_is_rejected(self):
    path = self.base / "first/f4-debug/main.bin.signed"
    data = path.read_bytes()[:-1] + bytes([path.read_bytes()[-1] ^ 1])
    path.write_bytes(data)
    self.build["artifacts"]["main.bin.signed"] = digest(data)
    for item in self.audit["inputs"]:
      if item["path"] == str(path):
        item["sha256"] = digest(data)
    with self.assertRaisesRegex(ValueError, "signature verification failed"):
      self.validate()

  def test_signature_key_claim_rejected(self):
    self.audit["signature"]["verified_with_public_key_sha256"] = "0" * 64
    with self.assertRaisesRegex(ValueError, "signature differs"):
      self.validate()

  def test_target_runtime_claim_rejected(self):
    self.audit["boot_cookies"]["runtime_impact_validated"] = True
    with self.assertRaisesRegex(ValueError, "impact claim"):
      self.validate()

  def test_empty_stack_frames(self):
    self.audit["stack"]["frames"] = []
    with self.assertRaisesRegex(ValueError, "stack frames"):
      self.validate()

  def test_incomplete_stack_cannot_be_promoted(self):
    self.audit["stack"]["direct_callgraph"]["complete_worst_case_bound"] = True
    with self.assertRaisesRegex(ValueError, "direct-stack evidence"):
      self.validate()

  def test_stack_sources_cannot_be_borrowed(self):
    self.audit["stack"]["direct_callgraph"]["files"][0]["path"] = str(self.base / "second/f4-debug/main.ci")
    with self.assertRaisesRegex(ValueError, "Stack source evidence"):
      self.validate()


if __name__ == "__main__":
  unittest.main()
