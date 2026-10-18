#!/usr/bin/env python3
"""Compare two independently invoked, application-only P5 candidate build sets.

No compiler, network, device access or signing private key is used here. The
orchestrator must record the actual clean build invocations: distinct manifests
alone cannot prove that a compiler ran. No normalization of firmware bytes is
permitted. ELF/map equality is reported separately from candidate byte equality.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import struct
from pathlib import Path, PurePosixPath

BUILD_NAMES = ("f4-debug", "f4-release", "h7-debug", "h7-release")
ARTIFACTS = frozenset(("build.log", "main.bin", "main.bin.signed", "main.ci", "main.d",
                       "main.elf", "main.map", "main.o", "main.su", "startup.o"))
CORE_ARTIFACTS = ("main.bin", "main.bin.signed")
DEV_PUBLIC_KEY_SHA256 = "98be6f5fd87ab61c51e91991628d6d9f0b3fd1ef448176e5610a55f18fd4e301"
SIGNATURE_LABEL = "public upstream DEVELOPMENT key, including release-policy builds"


def require(condition: bool, message: str) -> None:
  if not condition:
    raise ValueError(message)


def digest(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def read_regular(path: Path) -> bytes:
  require(path.is_file() and not path.is_symlink(), f"missing/nonregular/symlink artifact: {path}")
  return path.read_bytes()


def hash_map(value: object, label: str) -> dict:
  require(isinstance(value, dict) and bool(value), f"empty/invalid {label}")
  for name, checksum in value.items():
    require(isinstance(name, str) and bool(name) and not PurePosixPath(name).is_absolute()
            and ".." not in PurePosixPath(name).parts, f"unsafe {label} path")
    require(isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum) is not None,
            f"invalid {label} sha256")
  return value


def elf_image(data: bytes, raw: bytes, target: str) -> dict:
  """Check ARM executable/load payload without executing it or trusting a file label."""
  require(len(data) >= 52 and data[:7] == b"\x7fELF\x01\x01\x01", "ELF32 little endian required")
  kind, machine, version, entry, phoff, shoff, flags, ehsize, phsize, phnum, shsize, shnum, shstr = struct.unpack_from(
    "<HHIIIIIHHHHHH", data, 16)
  require((kind, machine, version, ehsize, phsize, shsize) == (2, 40, 1, 52, 32, 40), "invalid ARM executable header")
  require(flags & 0xFF000600 == 0x05000400, "ARM EABI5 hard-float executable required")
  require(phnum > 0 and shnum > 0 and shstr < shnum, "missing ELF tables")
  require(phoff >= 52 and phoff + phnum * phsize <= len(data), "truncated ELF program table")
  require(shoff >= 52 and shoff + shnum * shsize <= len(data), "truncated ELF section table")
  rows = [struct.unpack_from("<10I", data, shoff + i * shsize) for i in range(shnum)]
  names_row = rows[shstr]
  require(names_row[1] == 3 and names_row[4] + names_row[5] <= len(data), "invalid ELF section names")
  names = data[names_row[4]:names_row[4] + names_row[5]]
  sections = {}
  for row in rows:
    require(row[0] < len(names) and b"\0" in names[row[0]:], "invalid ELF section name")
    name = names[row[0]:].split(b"\0", 1)[0].decode("ascii")
    require(name not in sections, "duplicate ELF section")
    require(row[1] == 8 or row[4] + row[5] <= len(data), "truncated ELF section")
    sections[name] = row
  start = 0x08004000 if target == "f4" else 0x08020000
  require(".isr_vector" in sections and sections[".isr_vector"][3] == start, "wrong MCU vector address")
  require(".ARM.attributes" in sections, "missing ARM target attributes")
  attr = sections[".ARM.attributes"]
  attr_data = data[attr[4]:attr[4] + attr[5]]
  cpu = b"Cortex-M4\0" if target == "f4" else b"Cortex-M7\0"
  require(b"\x05" + cpu in attr_data, "wrong Cortex target attributes")
  require(8 <= len(raw) <= 0x08100000 - start - 136, "empty/oversize candidate")
  require(entry & 1 and start <= (entry & ~1) < start + len(raw), "invalid reset entry")
  sp, reset = struct.unpack_from("<II", raw)
  require(sp % 8 == 0 and 0x20000000 < sp <= 0x20020000 and reset == entry, "invalid raw reset vector")
  loads, program_layout = [], []
  for i in range(phnum):
    typ, offset, vaddr, paddr, filesz, memsz, pflags, align = struct.unpack_from("<8I", data, phoff + i * phsize)
    require(typ != 1 or filesz <= memsz, "ELF segment file length exceeds memory length")
    if typ == 1:
      program_layout.append({"vaddr": vaddr, "paddr": paddr, "file_bytes": filesz, "memory_bytes": memsz,
                             "flags": pflags, "alignment": align})
    if typ != 1 or not filesz:
      continue
    require(offset + filesz <= len(data), "truncated ELF load segment")
    require(start <= paddr < paddr + filesz <= start + len(raw), "ELF load outside candidate image")
    for other in loads:
      require(paddr + filesz <= other[0] or other[0] + other[1] <= paddr, "overlapping ELF loads")
    pos = paddr - start
    require(data[offset:offset + filesz] == raw[pos:pos + filesz], "ELF/raw load bytes differ")
    loads.append((paddr, filesz))
  require(bool(loads) and min(x[0] for x in loads) == start
          and max(x[0] + x[1] for x in loads) == start + len(raw), "ELF/raw image extent differs")
  return {"class": "ELF32", "endian": "little", "machine": "ARM", "type": "EXEC",
          "cpu": cpu[:-1].decode(), "entry": entry, "flash_start": start,
          "flags": flags, "load_segments": len(loads), "load_bytes_match_raw": True,
          "program_layout": program_layout,
          "allocated_sections": {name: {"type": row[1], "flags": row[2], "address": row[3],
                                         "bytes": row[5], "alignment": row[8]}
                                 for name, row in sections.items() if row[2] & 2}}


def public_rsa(public_key: Path) -> tuple[int, int]:
  contents = read_regular(public_key)
  require(digest(contents) == DEV_PUBLIC_KEY_SHA256, "wrong locked development public key")
  tokens = contents.split()
  require(len(tokens) >= 2 and tokens[0] == b"ssh-rsa", "invalid development public key")
  key = base64.b64decode(tokens[1], validate=True)
  values = []
  while key:
    require(len(key) >= 4, "truncated RSA key")
    count = struct.unpack_from(">I", key)[0]
    require(0 < count <= len(key) - 4, "invalid RSA key length")
    values.append(key[4:4 + count])
    key = key[4 + count:]
  require(len(values) == 3 and values[0] == b"ssh-rsa", "invalid RSA key fields")
  exponent, modulus = (int.from_bytes(x, "big") for x in values[1:])
  require(modulus.bit_length() == 1024 and exponent >= 3, "invalid RSA development key parameters")
  return exponent, modulus


def verify_signature(raw: bytes, signed: bytes, rsa: tuple[int, int]) -> None:
  require(len(signed) == len(raw) + 136, "signed image truncated or unexpected length")
  length = struct.unpack_from("<I", signed)[0]
  require(length == len(raw) + 8, "signed image length header mismatch")
  require(signed[4:len(raw)] == raw[4:] and signed[len(raw):length] == b"VERS\x02\0\0\0",
          "signed image payload/version mismatch")
  padding = b"\x00\x01" + b"\xff" * 0x69 + b"\x00" + hashlib.sha1(signed[4:length]).digest()
  exponent, modulus = rsa
  require(int.from_bytes(signed[length:], "big") < modulus, "RSA signature outside modulus")
  decoded = pow(int.from_bytes(signed[length:], "big"), exponent, modulus).to_bytes(128, "big")
  require(decoded == padding, "development signature mismatch")


def build_provenance(commands: object, directory: Path) -> dict:
  require(isinstance(commands, list) and len(commands) == 6, "unexpected build command transcript")
  require(all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in commands),
          "invalid build command transcript")
  compiles = [c for c in commands if "-c" in c and "-o" in c]
  require(len(compiles) == 2, "missing compile command pair")
  main = [c for c in compiles if c[c.index("-c") + 1].endswith("/board/main.c")]
  require(len(main) == 1, "missing main compilation")
  main = main[0]
  source = Path(main[main.index("-c") + 1])
  output = Path(main[main.index("-o") + 1])
  require(source.is_absolute() and output.is_absolute(), "build paths must be absolute")
  require(output.resolve() == directory / "main.o", "build transcript points at another output directory")
  source_root = source.parent.parent.resolve()
  expected_maps = (f"-ffile-prefix-map={source_root}=panda", f"-ffile-prefix-map={directory}=build")
  require(all(flag in main for flag in expected_maps), "missing deterministic source/output path mapping")
  dbc = [flag[len("-ffile-prefix-map="):-len("=opendbc")] for flag in main
         if flag.startswith("-ffile-prefix-map=") and flag.endswith("=opendbc")]
  require(len(dbc) == 1 and Path(dbc[0]).is_absolute(), "missing deterministic opendbc mapping")
  return {"source_root": str(source_root), "opendbc_root": str(Path(dbc[0]).resolve()),
          "output_root": str(directory), "compiler_path": main[0]}


def validate_build(directory: Path, target: str, policy: str, rsa: tuple[int, int]) -> dict:
  directory = directory.resolve()
  build = json.loads(read_regular(directory / "build.json"))
  require(build.get("schema") == "songplus-p5-app-build/v1", "unexpected build schema")
  require(build.get("target") == target and build.get("policy") == policy, "wrong target/policy metadata")
  require(build.get("deployable") is False and build.get("bootstub_built") is False, "nondeployable app-only boundary changed")
  require(build.get("signature_key") == SIGNATURE_LABEL, "signature provenance changed")
  require(isinstance(build.get("compiler"), str) and build["compiler"].startswith("arm-none-eabi-gcc "), "wrong compiler metadata")
  board = hash_map(build.get("board_inputs"), "board_inputs")
  safety = hash_map(build.get("safety_inputs"), "safety_inputs")
  identity = digest(json.dumps({"board": board, "safety": safety}, sort_keys=True).encode())
  require(build.get("source_identity") == identity, "source identity mismatch")
  version = f"P5-{target}-{identity[:12]}-{policy}"
  require(build.get("version") == version, "firmware version mismatch")
  protocol = hash_map(build.get("protocol_inputs"), "protocol_inputs")
  require(protocol == {"board/health.h": board.get("board/health.h"),
                       "opendbc/safety/can.h": safety.get("opendbc/safety/can.h")}, "protocol/source identity mismatch")
  declared = hash_map(build.get("artifacts"), "artifacts")
  require(set(declared) == ARTIFACTS, "missing/unexpected declared artifacts")
  contents = {}
  for name, checksum in declared.items():
    contents[name] = read_regular(directory / name)
    require(bool(contents[name]) and digest(contents[name]) == checksum, f"artifact hash mismatch: {name}")
  raw, signed = (contents[name] for name in CORE_ARTIFACTS)
  require(raw.count(version.encode() + b"\0") == 1, "raw image firmware version missing/ambiguous")
  elf = elf_image(contents["main.elf"], raw, target)
  verify_signature(raw, signed, rsa)
  provenance = build_provenance(build.get("commands"), directory)
  return {"target": target, "policy": policy, "compiler": build["compiler"], "version": version,
          "source_identity": identity, "protocol_inputs": protocol, "provenance": provenance,
          "artifacts": {name: {"sha256": digest(data), "bytes": len(data)} for name, data in contents.items()},
          "elf": elf, "signature_verified": True, "deployable": False, "bootstub_built": False}


def compare_build_sets(first: Path, second: Path, public_key: Path) -> dict:
  first, second = first.resolve(), second.resolve()
  require(first != second and not first.is_relative_to(second) and not second.is_relative_to(first),
          "two independent, nonnested build roots required")
  rsa = public_rsa(public_key)
  results = {}
  for name in BUILD_NAMES:
    target, policy = name.split("-")
    left = validate_build(first / name, target, policy, rsa)
    right = validate_build(second / name, target, policy, rsa)
    for field in ("source_identity", "version", "compiler", "protocol_inputs", "elf"):
      require(left[field] == right[field], f"{name}: {field} differs")
    for field in ("source_root", "opendbc_root", "output_root"):
      require(left["provenance"][field] != right["provenance"][field], f"{name}: {field} was reused")
    equal = {}
    for artifact in sorted(ARTIFACTS):
      lp, rp = first / name / artifact, second / name / artifact
      require(not lp.samefile(rp), f"{name}: artifact aliases the first build: {artifact}")
      equal[artifact] = lp.read_bytes() == rp.read_bytes()
      if artifact in CORE_ARTIFACTS:
        require(equal[artifact], f"{name}: reproducibility drift in {artifact}")
    results[name] = {"first": left, "second": right, "byte_equal": equal,
                     "core_candidate_bytes_equal": True}
  require(len({item["first"]["source_identity"] for item in results.values()}) == 1,
          "four variants must use one source identity")
  require(len({item["first"]["compiler"] for item in results.values()}) == 1,
          "four variants must use one compiler identity")
  return {"schema": "songplus-p7-firmware-reproducibility/v1", "status": "PASS",
          "build_variants": len(results), "candidate_builds_compared": 2 * len(results),
          "required_byte_equal_files": list(CORE_ARTIFACTS), "results": results,
          "scope": "Two clean source/output paths; exact raw and development-signed application bytes. Actual compiler invocations must be recorded by the orchestrator.",
          "whole_directory_byte_reproducibility_claimed": False, "cross_toolchain_reproducibility_claimed": False,
          "deployable": False, "device_access": False, "mcu_executed": False}


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--first", type=Path, required=True)
  parser.add_argument("--second", type=Path, required=True)
  parser.add_argument("--public-key", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  require(not args.output.exists(), "comparison output already exists; choose a fresh report path")
  report = compare_build_sets(args.first, args.second, args.public_key)
  args.output.write_text(json.dumps(report, indent=2) + "\n")
  print(json.dumps({key: report[key] for key in ("status", "build_variants", "candidate_builds_compared")}))


if __name__ == "__main__":
  main()
