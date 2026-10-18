"""P7 A70/A72 stdlib tests. No official checkout or hardware library is needed."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import runtime_guard as guard


def make_artifact_fixture(root: Path) -> tuple[dict, dict]:
  """Minimal valid 12-file fixture; payloads deliberately are not firmware."""
  records = []
  for mcu in ("F4", "H7"):
    for variant in ("debug", "release"):
      for kind in ("firmware-elf", "firmware-bin", "firmware-signed-bin"):
        name = f"{mcu.lower()}/{variant}/{kind}.fixture"
        payload = f"SYNTHETIC NONEXECUTABLE {mcu} {variant} {kind}\n".encode()
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        records.append({
          "path": name, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
          "kind": kind, "mcu": mcu, "build_variant": variant, "protocol": guard.PROTOCOL,
          "signature": "public-development-key" if kind == "firmware-signed-bin" else "unsigned",
          "deployable": False,
        })
  return guard.synthetic_inventory(), {
    "schema": "songplus-maintenance-artifacts/v1", "baseline_sha": guard.BASELINE_SHA,
    "deployable": False, "permissions": dict(guard.DENIED_PERMISSIONS), "artifacts": records,
  }


def make_evidence() -> tuple[list[dict], dict]:
  context = {"run_id": "fixture-current-run", "lock_sha256": "c" * 64}
  cases = [{"id": case_id, "status": "PASS", "executed": 1, "failed": 0, "skipped": 0,
            "evidence_sha256": hashlib.sha256(case_id.encode()).hexdigest(), **context} for case_id in guard.REQUIRED_CASE_IDS]
  return cases, context


_AUDIT_ACTIVE = False
_AUDIT_FORBIDDEN = []


def _audit_hook(event, args):
  if not _AUDIT_ACTIVE:
    return
  forbidden = event.startswith(("subprocess.", "socket.", "ctypes.dlopen", "os.exec", "os.spawn", "os.posix_spawn", "os.fork")) or event == "os.system"
  if event == "open" and args and isinstance(args[0], str):
    forbidden = forbidden or args[0] == "/dev" or args[0].startswith(("/dev/", "/sys/", "/proc/"))
  if forbidden:
    _AUDIT_FORBIDDEN.append(event)
    raise AssertionError("forbidden audit event: " + event)


sys.addaudithook(_audit_hook)


class RuntimeGuardTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix="songplus-p7-guard-")
    # macOS /var is a symlink; the guard deliberately requires a physical path.
    self.root = Path(self.temp.name).resolve()
    self.inventory, self.manifest = make_artifact_fixture(self.root)

  def tearDown(self):
    self.temp.cleanup()

  def inspect(self, inventory=None, manifest=None, root=None):
    return guard.inspect_runtime(self.inventory if inventory is None else inventory,
                                 self.manifest if manifest is None else manifest,
                                 self.root if root is None else root)

  def assert_denied(self, result):
    self.assertFalse(result["inspection_valid"])
    self.assertFalse(result["offline_artifacts_available"])
    self.assertTrue(result["reasons"])
    self.assert_closed(result)

  def assert_closed(self, result):
    self.assertIs(result["runtime_available"], False)
    self.assertEqual(result["permissions"], guard.DENIED_PERMISSIONS)
    self.assertTrue(all(value is False for value in result["permissions"].values()))

  def test_valid_files_are_available_only_for_offline_inspection(self):
    result = self.inspect()
    self.assertTrue(result["inspection_valid"])
    self.assertTrue(result["offline_artifacts_available"])
    self.assertEqual(result["reasons"], [])
    self.assertEqual(len(result["artifacts"]), 12)
    self.assert_closed(result)

  def test_fixed_slot_permutation_and_h7_spi_do_not_change_permissions(self):
    for dos_slot in (0, 1):
      for transport in ("USB", "SPI"):
        with self.subTest(dos_slot=dos_slot, transport=transport):
          inventory = guard.synthetic_inventory(dos_slot)
          inventory["slots"][1 - dos_slot]["transport"] = transport
          result = self.inspect(inventory=inventory)
          self.assertTrue(result["inspection_valid"])
          self.assert_closed(result)

  def test_unknown_board_mcu_protocol_signature_transport_and_identity(self):
    fields = {"identity": "real:serial", "board": "UNKNOWN", "mcu": "F413-like", "exact_mcu": "STM32F413",
              "protocol": "100405", "signature": "release-key", "transport": "SPI"}
    for field, value in fields.items():
      with self.subTest(field=field):
        inventory = copy.deepcopy(self.inventory)
        inventory["slots"][0][field] = value
        self.assert_denied(self.inspect(inventory=inventory))

  def test_inventory_missing_extra_duplicate_reordered_or_physical_denied(self):
    variants = []
    for slots in ([], self.inventory["slots"][:1], self.inventory["slots"] * 2,
                  [self.inventory["slots"][0]] * 2, list(reversed(self.inventory["slots"]))):
      variants.append({**self.inventory, "slots": slots})
    variants += [{**self.inventory, "synthetic": False}, {**self.inventory, "synthetic": 1},
                 {**self.inventory, "device_address": "device.invalid"}]
    for variant in variants:
      with self.subTest(variant=variant):
        self.assert_denied(self.inspect(inventory=variant))

  def test_malformed_inventory_never_coerces_types(self):
    for value in (None, [], True, 0, "fixture"):
      self.assert_denied(guard.inspect_runtime(value, self.manifest, self.root))
    for field in self.inventory["slots"][0]:
      for value in (None, [], {}, True):
        with self.subTest(field=field, value=value):
          inventory = copy.deepcopy(self.inventory)
          inventory["slots"][0][field] = value
          self.assert_denied(self.inspect(inventory=inventory))

  def test_incomplete_artifact_build_denied(self):
    for missing in range(len(self.manifest["artifacts"])):
      manifest = copy.deepcopy(self.manifest)
      del manifest["artifacts"][missing]
      self.assert_denied(self.inspect(manifest=manifest))

  def test_changed_missing_truncated_grown_or_wrong_hash_files_denied(self):
    item = self.manifest["artifacts"][0]
    target = self.root / item["path"]
    original = target.read_bytes()
    for change in (original[:-1], original + b"X", bytes([original[0] ^ 1]) + original[1:]):
      target.write_bytes(change)
      self.assert_denied(self.inspect())
    target.unlink()
    self.assert_denied(self.inspect())

  def test_manifest_pin_protocol_signatures_and_permission_escalation_denied(self):
    for field, value in (("baseline_sha", "0" * 40), ("deployable", True), ("schema", "unknown")):
      manifest = copy.deepcopy(self.manifest)
      manifest[field] = value
      self.assert_denied(self.inspect(manifest=manifest))
    for permission in guard.DENIED_PERMISSIONS:
      manifest = copy.deepcopy(self.manifest)
      manifest["permissions"][permission] = True
      self.assert_denied(self.inspect(manifest=manifest))
    for field, value in (("protocol", "100405"), ("signature", "release-key"), ("deployable", True),
                         ("mcu", "F7"), ("build_variant", "production"), ("kind", "bootstub")):
      manifest = copy.deepcopy(self.manifest)
      manifest["artifacts"][0][field] = value
      self.assert_denied(self.inspect(manifest=manifest))

  def test_malformed_artifact_manifest_and_records_denied(self):
    for value in (None, [], True, 0, "manifest", {**self.manifest, "launch": "pandad"}):
      self.assert_denied(guard.inspect_runtime(self.inventory, value, self.root))
    for value in (None, {}, "list", [], [None], [True]):
      self.assert_denied(self.inspect(manifest={**self.manifest, "artifacts": value}))
    for field in self.manifest["artifacts"][0]:
      for value in (None, [], {}, True):
        manifest = copy.deepcopy(self.manifest)
        manifest["artifacts"][0][field] = value
        self.assert_denied(self.inspect(manifest=manifest))

  def test_artifact_path_traversal_absolute_and_uri_denied_before_open(self):
    for name in ("../outside", "/dev/null", "file:/tmp/payload", "https://invalid/payload", "a//b", "./file", "a/../file", "a\\b", "nul\x00name"):
      with self.subTest(name=name):
        manifest = copy.deepcopy(self.manifest)
        manifest["artifacts"][0]["path"] = name
        with patch.object(guard.os, "open", side_effect=AssertionError("must not open")):
          self.assert_denied(self.inspect(manifest=manifest))

  def test_duplicate_paths_and_mcu_variant_kinds_denied(self):
    for field in ("path", "identity"):
      manifest = copy.deepcopy(self.manifest)
      duplicate = copy.deepcopy(manifest["artifacts"][0])
      if field == "identity":
        duplicate["path"] = "unique-but-same-kind"
      manifest["artifacts"].append(duplicate)
      self.assert_denied(self.inspect(manifest=manifest))

  def test_zero_negative_bool_and_oversized_artifact_sizes_denied(self):
    for size in (0, -1, True, 1.0, 64 * 1024 * 1024 + 1):
      manifest = copy.deepcopy(self.manifest)
      manifest["artifacts"][0]["size_bytes"] = size
      self.assert_denied(self.inspect(manifest=manifest))

  def test_leaf_symlink_hardlink_and_fifo_never_read_as_artifact(self):
    target = self.root / self.manifest["artifacts"][0]["path"]
    original = target.read_bytes()
    target.unlink()
    outside = self.root / "outside"
    outside.write_bytes(original)
    target.symlink_to(outside)
    self.assert_denied(self.inspect())
    target.unlink()
    os.link(outside, target)
    self.assert_denied(self.inspect())
    target.unlink()
    os.mkfifo(target)
    self.assert_denied(self.inspect())

  def test_symlink_directory_and_root_rejected(self):
    directory = self.root / "f4"
    directory.rename(self.root / "real-f4")
    directory.symlink_to(self.root / "real-f4", target_is_directory=True)
    self.assert_denied(self.inspect())
    root_alias = self.root / "root-alias"
    root_alias.symlink_to(self.root, target_is_directory=True)
    self.assert_denied(self.inspect(root=root_alias))
    self.assert_denied(self.inspect(root=root_alias / "real-f4"))

  def test_device_root_rejected_before_open(self):
    for root in ("/dev", "/dev/null", "/proc", "/proc/self", "/sys", "/sys/devices", "/"):
      with patch.object(guard.os, "open", side_effect=AssertionError("must not open")):
        self.assert_denied(self.inspect(root=root))

  def test_file_changed_between_stat_and_open_denied(self):
    original_open = os.open
    attacked = False

    def changing_open(path, flags, *args, **kwargs):
      nonlocal attacked
      if path == "firmware-elf.fixture" and not attacked:
        target = self.root / self.manifest["artifacts"][0]["path"]
        target.unlink()
        target.write_bytes(b"Y" * self.manifest["artifacts"][0]["size_bytes"])
        attacked = True
      return original_open(path, flags, *args, **kwargs)

    with patch.object(guard.os, "open", side_effect=changing_open):
      self.assert_denied(self.inspect())
    self.assertTrue(attacked)

  def test_invalid_inventory_never_reads_artifacts(self):
    inventory = copy.deepcopy(self.inventory)
    inventory["slots"][0]["signature"] = "unknown"
    with patch.object(guard.os, "open", side_effect=AssertionError("must not open")):
      self.assert_denied(self.inspect(inventory=inventory))

  def test_import_and_inspection_have_zero_subprocess_device_network_calls(self):
    global _AUDIT_ACTIVE
    direct = []

    def forbidden(name):
      def fail(*_args, **_kwargs):
        direct.append(name)
        raise AssertionError("forbidden direct operation: " + name)
      return fail

    fake_panda = types.ModuleType("panda")
    fake_panda.Panda = forbidden("Panda.constructor")
    fake_panda.PandaDFU = forbidden("PandaDFU.constructor")
    fake_usb = types.ModuleType("usb1")
    fake_usb.USBContext = forbidden("USBContext.constructor")
    fake_serial = types.ModuleType("serial")
    fake_serial.Serial = forbidden("Serial.constructor")
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
      if name.split(".")[0] in ("panda", "usb1", "libusb1", "serial"):
        return forbidden("device-module-import:" + name)()
      return original_import(name, *args, **kwargs)

    original_open = os.open

    def device_open(path, *args, **kwargs):
      if isinstance(path, (str, Path)) and str(path).startswith(("/dev/", "/sys/", "/proc/")):
        return forbidden("device-open")()
      return original_open(path, *args, **kwargs)

    with ExitStack() as stack:
      stack.enter_context(patch.dict(sys.modules, {"panda": fake_panda, "usb1": fake_usb, "serial": fake_serial}))
      for name in ("Popen", "run", "call", "check_call", "check_output"):
        stack.enter_context(patch.object(subprocess, name, forbidden("subprocess." + name)))
      for name in ("system", "posix_spawn", "posix_spawnp", "fork", "execv", "execve", "execvp", "execvpe"):
        if hasattr(os, name):
          stack.enter_context(patch.object(os, name, forbidden("os." + name)))
      stack.enter_context(patch.object(socket, "socket", forbidden("socket.socket")))
      stack.enter_context(patch.object(os, "open", device_open))
      stack.enter_context(patch("builtins.__import__", guarded_import))
      # These deliberate intercepted calls prove direct spies are active; none
      # reaches the OS or an actual device library.
      for positive_control in (lambda: subprocess.run(["forbidden"], check=True), fake_panda.Panda,
                               fake_panda.PandaDFU, fake_usb.USBContext, fake_serial.Serial,
                               lambda: os.open("/dev/forbidden", os.O_RDONLY), lambda: socket.socket()):
        with self.assertRaises(AssertionError):
          positive_control()
      self.assertEqual(len(direct), 7)
      direct.clear()
      _AUDIT_FORBIDDEN.clear()
      _AUDIT_ACTIVE = True
      try:
        # Explicit audit signals prove the hook independently of mocked APIs.
        for event in ("subprocess.Popen", "socket.connect", "os.system", "open"):
          with self.assertRaises(AssertionError):
            sys.audit(event, "/dev/forbidden")
        self.assertEqual(len(_AUDIT_FORBIDDEN), 4)
        _AUDIT_FORBIDDEN.clear()
        spec = importlib.util.spec_from_file_location("guard_fresh_import", guard.__file__)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        self.assertTrue(fresh.inspect_runtime(self.inventory, self.manifest, self.root)["inspection_valid"])
        inventory = copy.deepcopy(self.inventory)
        inventory["slots"][0]["signature"] = "unknown"
        self.assert_denied(fresh.inspect_runtime(inventory, self.manifest, self.root))
        missing = copy.deepcopy(self.manifest)
        missing["artifacts"][0]["path"] = "missing-artifact"
        self.assert_denied(fresh.inspect_runtime(self.inventory, missing, self.root))
        cases, context = make_evidence()
        self.assertFalse(fresh.evaluate_release(cases, evidence_context=context)["deployment_ready"])
      finally:
        _AUDIT_ACTIVE = False
      self.assertEqual(direct, [])
      self.assertEqual(_AUDIT_FORBIDDEN, [])


class ReleaseGateTests(unittest.TestCase):
  def setUp(self):
    self.cases, self.context = make_evidence()

  def release(self, cases=None, context=None):
    return guard.evaluate_release(self.cases if cases is None else cases,
                                  evidence_context=self.context if context is None else context)

  def assert_closed(self, result):
    self.assertIs(result["deployment_ready"], False)
    self.assertIs(result["release_installable"], False)
    self.assertEqual(result["permissions"], guard.DENIED_PERMISSIONS)
    self.assertEqual(len(result["blockers"]), 8)
    self.assertTrue(all(item["id"] and item["status"] and item["detail"] for item in result["blockers"]))
    self.assertTrue(all(value is False for key, value in result["scopes"].items() if key != "offline_host_and_arm_build"))

  def test_all_41_current_cases_pass_only_offline_scope(self):
    result = self.release()
    self.assertTrue(result["offline_compatibility_complete"])
    self.assertEqual(result["offline_cases_passed"], 41)
    self.assertEqual(result["offline_evidence_errors"], [])
    self.assert_closed(result)

  def test_each_missing_case_and_duplicate_prevents_offline_completion(self):
    for index in range(len(self.cases)):
      cases = copy.deepcopy(self.cases)
      del cases[index]
      self.assertFalse(self.release(cases=cases)["offline_compatibility_complete"])
    cases = copy.deepcopy(self.cases)
    cases[-1] = cases[0]
    self.assertFalse(self.release(cases=cases)["offline_compatibility_complete"])

  def test_fail_skip_zero_notrun_and_bad_counts_cannot_pass(self):
    for key, value in (("status", "NOT_RUN"), ("status", "PASS_OFFLINE_DECLARED_SCOPE"), ("failed", 1), ("skipped", 1),
                       ("executed", 0), ("executed", True), ("executed", 1.0), ("failed", False), ("skipped", -1),
                       ("evidence_sha256", "unknown")):
      cases = copy.deepcopy(self.cases)
      cases[0][key] = value
      result = self.release(cases=cases)
      self.assertFalse(result["offline_compatibility_complete"])
      self.assert_closed(result)

  def test_old_run_foreign_lock_and_missing_context_cannot_pass(self):
    for key, value in (("run_id", "historical-run"), ("lock_sha256", "d" * 64)):
      cases = copy.deepcopy(self.cases)
      cases[0][key] = value
      self.assertFalse(self.release(cases=cases)["offline_compatibility_complete"])
    for context in (None, {}, [], {"run_id": "bad/run", "lock_sha256": "c" * 64}, {"run_id": "ok", "lock_sha256": "bad"}):
      self.assertFalse(guard.evaluate_release(self.cases, evidence_context=context)["offline_compatibility_complete"])

  def test_generic_verified_flags_do_not_remove_physical_blockers(self):
    context = {**self.context, "all_physical_conditions_verified": True, "deployment_ready": True,
               "recovery_verified": True, "driver_monitoring": True, "vehicle_validation": True,
               "permissions": dict.fromkeys(guard.DENIED_PERMISSIONS, True), "blockers": []}
    result = self.release(context=context)
    self.assertTrue(result["offline_compatibility_complete"])
    self.assert_closed(result)
    self.assertEqual(result["blockers"], self.release()["blockers"])

  def test_malformed_case_inputs_fail_closed(self):
    for value in (None, True, {}, "PASS", [], [None] * 41, [{}] * 41, self.cases * 4):
      result = guard.evaluate_release(value, evidence_context=self.context)
      self.assertFalse(result["offline_compatibility_complete"])
      self.assert_closed(result)
    for field in self.cases[0]:
      for value in (None, [], {}, True):
        cases = copy.deepcopy(self.cases)
        cases[0][field] = value
        self.assertFalse(self.release(cases=cases)["offline_compatibility_complete"])

  def test_policy_documents_exact_executable_gate_set(self):
    policy = guard.load_release_policy()
    self.assertEqual(policy["baseline_sha"], guard.BASELINE_SHA)
    self.assertEqual(policy["required_offline_case_ids"], list(guard.REQUIRED_CASE_IDS))
    self.assertEqual(policy["deployment"]["blocked_gate_ids"], [item["id"] for item in self.release()["blockers"]])
    self.assertEqual(set(policy["forbidden_automatic_actions"]), set(guard.DENIED_PERMISSIONS))
    self.assertFalse(policy["deployment"]["can_be_enabled_by_evidence_or_configuration"])

  def test_modified_policy_data_cannot_grant_permission(self):
    with patch.object(guard, "load_release_policy", return_value={"deployment": {"ready": True}, "blockers": []}):
      self.assert_closed(self.release())


if __name__ == "__main__":
  unittest.main(verbosity=2)
