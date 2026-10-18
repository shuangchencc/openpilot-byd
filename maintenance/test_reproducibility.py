"""Small synthetic parser/gate fixtures; these are NOT compiler or ARM executions.

The synthetic signature has an identity RSA operation to exercise envelope and
digest validation without a private key. Formal validation uses the unchanged
locked public key on all eight actual candidate builds.
"""
from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance import reproducibility as repro

SYNTHETIC_RSA = (1, (1 << 1024) - 109)


def signed_fixture(raw: bytes) -> bytes:
  body = struct.pack("<I", len(raw) + 8) + raw[4:] + b"VERS\x02\0\0\0"
  return body + b"\x00\x01" + b"\xff" * 0x69 + b"\0" + hashlib.sha1(body[4:]).digest()


def elf_fixture(raw: bytes, target: str) -> bytes:
  start = 0x08004000 if target == "f4" else 0x08020000
  cpu = b"Cortex-M4\0" if target == "f4" else b"Cortex-M7\0"
  attrs = b"A\x05" + cpu
  names = b"\0.isr_vector\0.ARM.attributes\0.shstrtab\0"
  raw_offset = 84
  attr_offset = raw_offset + len(raw)
  names_offset = attr_offset + len(attrs)
  section_offset = names_offset + len(names)
  ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
  header = ident + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, start + 9, 52, section_offset,
                               0x05000400, 52, 32, 1, 40, 4, 3)
  program = struct.pack("<8I", 1, raw_offset, start, start, len(raw), len(raw), 5, 4)
  rows = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
          (names.index(b".isr_vector"), 1, 2, start, raw_offset, len(raw), 0, 0, 4, 0),
          (names.index(b".ARM.attributes"), 0x70000003, 0, 0, attr_offset, len(attrs), 0, 0, 1, 0),
          (names.index(b".shstrtab"), 3, 0, 0, names_offset, len(names), 0, 0, 1, 0)]
  return header + program + raw + attrs + names + b"".join(struct.pack("<10I", *row) for row in rows)


def write_fixture(root: Path, name: str) -> Path:
  target, policy = name.split("-")
  output = root / name
  output.mkdir(parents=True)
  source, dbc = root / "panda", root / "opendbc"
  source.mkdir(exist_ok=True)
  dbc.mkdir(exist_ok=True)
  board = {"board/health.h": repro.digest(b"health fixture")}
  safety = {"opendbc/safety/can.h": repro.digest(b"CAN fixture")}
  identity = repro.digest(json.dumps({"board": board, "safety": safety}, sort_keys=True).encode())
  version = f"P5-{target}-{identity[:12]}-{policy}"
  start = 0x08004000 if target == "f4" else 0x08020000
  raw = struct.pack("<II", 0x2001FFF8, start + 9) + bytes(24) + version.encode() + b"\0" + bytes(64)
  (output / "main.bin").write_bytes(raw)
  (output / "main.bin.signed").write_bytes(signed_fixture(raw))
  (output / "main.elf").write_bytes(elf_fixture(raw, target))
  for artifact in repro.ARTIFACTS - {"main.bin", "main.bin.signed", "main.elf"}:
    (output / artifact).write_text(f"Synthetic fixture only; output {output}; {artifact}\n")
  common = ["/fixture/arm-none-eabi-gcc", f"-ffile-prefix-map={source}=panda",
            f"-ffile-prefix-map={dbc}=opendbc", f"-ffile-prefix-map={output}=build"]
  commands = [[common[0], "--version"],
              common + ["-c", str(source / "board/main.c"), "-o", str(output / "main.o")],
              common + ["-c", str(source / "board/startup.s"), "-o", str(output / "startup.o")],
              common + ["-o", str(output / "main.elf")],
              ["/fixture/objcopy", str(output / "main.elf"), str(output / "main.bin")],
              ["/fixture/python", "fixture-signing-transcript-only"]]
  build = {"schema": "songplus-p5-app-build/v1", "target": target, "policy": policy,
           "compiler": "arm-none-eabi-gcc SYNTHETIC FIXTURE", "version": version, "source_identity": identity,
           "board_inputs": board, "safety_inputs": safety, "protocol_inputs": board | safety,
           "deployable": False, "bootstub_built": False, "signature_key": repro.SIGNATURE_LABEL,
           "commands": commands, "artifacts": {}}
  (output / "build.json").write_text(json.dumps(build))
  refresh_hashes(output)
  return output


def refresh_hashes(output: Path) -> None:
  metadata = output / "build.json"
  build = json.loads(metadata.read_text())
  build["artifacts"] = {name: repro.digest((output / name).read_bytes()) for name in sorted(repro.ARTIFACTS)
                        if (output / name).exists()}
  metadata.write_text(json.dumps(build))


class ReproducibilityTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name).resolve()
    self.first, self.second = self.base / "first", self.base / "second"
    for root in (self.first, self.second):
      for name in repro.BUILD_NAMES:
        write_fixture(root, name)
    self.path = self.second / "f4-debug"

  def compare(self):
    with patch.object(repro, "public_rsa", return_value=SYNTHETIC_RSA):
      return repro.compare_build_sets(self.first, self.second, self.base / "synthetic-public-key")

  def metadata(self, mutate):
    path = self.path / "build.json"
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value))

  def test_four_variants_exact_core_only(self):
    result = self.compare()
    self.assertEqual(result["status"], "PASS")
    self.assertEqual(result["candidate_builds_compared"], 8)
    self.assertFalse(result["whole_directory_byte_reproducibility_claimed"])
    for variant in result["results"].values():
      self.assertTrue(variant["byte_equal"]["main.bin"])
      self.assertTrue(variant["byte_equal"]["main.bin.signed"])
      self.assertFalse(variant["byte_equal"]["main.map"])

  def test_raw_hash_drift_rejected(self):
    path = self.path / "main.bin"
    path.write_bytes(path.read_bytes() + b"drift")
    with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
      self.compare()

  def test_coherent_firmware_drift_is_not_normalized_away(self):
    path = self.path / "main.bin"
    raw = path.read_bytes()[:-1] + b"\x01"
    path.write_bytes(raw)
    (self.path / "main.bin.signed").write_bytes(signed_fixture(raw))
    (self.path / "main.elf").write_bytes(elf_fixture(raw, "f4"))
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "reproducibility drift in main.bin"):
      self.compare()

  def test_truncated_signed_image_rejected(self):
    path = self.path / "main.bin.signed"
    path.write_bytes(path.read_bytes()[:-1])
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "signed image truncated"):
      self.compare()

  def test_invalid_signature_rejected(self):
    path = self.path / "main.bin.signed"
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "signature mismatch"):
      self.compare()

  def test_wrong_target_metadata_rejected(self):
    self.metadata(lambda b: b.update(target="h7"))
    with self.assertRaisesRegex(ValueError, "wrong target/policy"):
      self.compare()

  def test_wrong_target_elf_rejected(self):
    raw = (self.path / "main.bin").read_bytes()
    (self.path / "main.elf").write_bytes(elf_fixture(raw, "h7"))
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "wrong MCU vector"):
      self.compare()

  def test_wrong_cortex_attributes_rejected(self):
    path = self.path / "main.elf"
    path.write_bytes(path.read_bytes().replace(b"Cortex-M4", b"Cortex-M7"))
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "wrong Cortex"):
      self.compare()

  def test_native_or_relocatable_elf_rejected(self):
    path = self.path / "main.elf"
    raw = bytearray(path.read_bytes())
    struct.pack_into("<H", raw, 16, 1)
    path.write_bytes(raw)
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "invalid ARM executable"):
      self.compare()

  def test_truncated_elf_rejected(self):
    (self.path / "main.elf").write_bytes(b"\x7fELF\x01\x01\x01")
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "ELF32"):
      self.compare()

  def test_elf_load_payload_drift_rejected(self):
    path = self.path / "main.elf"
    raw = bytearray(path.read_bytes())
    raw[100] ^= 1
    path.write_bytes(raw)
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "ELF/raw load bytes differ"):
      self.compare()

  def test_memory_layout_drift_with_equal_raw_is_rejected(self):
    path = self.path / "main.elf"
    raw = bytearray(path.read_bytes())
    memsz = struct.unpack_from("<I", raw, 52 + 20)[0]
    struct.pack_into("<I", raw, 52 + 20, memsz + 4)
    path.write_bytes(raw)
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "elf differs"):
      self.compare()

  def test_embedded_firmware_version_required(self):
    path = self.path / "main.bin"
    raw = path.read_bytes().replace(b"P5-f4", b"P5-XX")
    path.write_bytes(raw)
    (self.path / "main.bin.signed").write_bytes(signed_fixture(raw))
    (self.path / "main.elf").write_bytes(elf_fixture(raw, "f4"))
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "firmware version missing"):
      self.compare()

  def test_wrong_firmware_version_rejected(self):
    self.metadata(lambda b: b.update(version="unexpected"))
    with self.assertRaisesRegex(ValueError, "firmware version mismatch"):
      self.compare()

  def test_false_source_identity_rejected(self):
    self.metadata(lambda b: b.update(source_identity="0" * 64))
    with self.assertRaisesRegex(ValueError, "source identity mismatch"):
      self.compare()

  def test_unsupported_deployable_claim_rejected(self):
    self.metadata(lambda b: b.update(deployable=True))
    with self.assertRaisesRegex(ValueError, "app-only boundary changed"):
      self.compare()

  def test_missing_variant_rejected(self):
    (self.second / "h7-release/build.json").unlink()
    with self.assertRaisesRegex(ValueError, "missing/nonregular"):
      self.compare()

  def test_missing_artifact_rejected(self):
    (self.path / "main.map").unlink()
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "missing/unexpected declared"):
      self.compare()

  def test_unsafe_artifact_path_rejected(self):
    self.metadata(lambda b: b["artifacts"].update({"../outside": "0" * 64}))
    with self.assertRaisesRegex(ValueError, "unsafe artifacts path"):
      self.compare()

  def test_symlink_artifact_rejected(self):
    path = self.path / "main.bin"
    path.unlink()
    path.symlink_to(self.first / "f4-debug/main.bin")
    with self.assertRaisesRegex(ValueError, "missing/nonregular/symlink"):
      self.compare()

  def test_hardlink_alias_rejected(self):
    path = self.path / "main.bin"
    path.unlink()
    path.hardlink_to(self.first / "f4-debug/main.bin")
    with self.assertRaisesRegex(ValueError, "artifact aliases"):
      self.compare()

  def test_same_build_root_rejected(self):
    with self.assertRaisesRegex(ValueError, "independent, nonnested"):
      repro.compare_build_sets(self.first, self.first, self.base / "unused")

  def test_reused_source_root_rejected(self):
    def reuse(b):
      b["commands"] = [[arg.replace(str(self.second / "panda"), str(self.first / "panda")) for arg in cmd]
                       for cmd in b["commands"]]
    self.metadata(reuse)
    with self.assertRaisesRegex(ValueError, "source_root was reused"):
      self.compare()

  def test_copied_build_manifest_rejected(self):
    (self.path / "build.json").write_bytes((self.first / "f4-debug/build.json").read_bytes())
    refresh_hashes(self.path)
    with self.assertRaisesRegex(ValueError, "another output directory"):
      self.compare()

  def test_wrong_public_key_rejected(self):
    path = self.base / "wrong.pub"
    path.write_text("ssh-rsa invalid fixture key\n")
    with self.assertRaisesRegex(ValueError, "wrong locked development public key"):
      repro.public_rsa(path)


if __name__ == "__main__":
  unittest.main()
