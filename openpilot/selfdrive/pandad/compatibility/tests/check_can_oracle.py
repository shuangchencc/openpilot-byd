#!/usr/bin/env python3
"""Independent wire formula versus the native codec CLI, no device APIs."""
import argparse
import json
from pathlib import Path
import random
import struct
import subprocess


LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def wire(frame):
  payload = bytes.fromhex(frame["payload_hex"])
  byte0 = LENGTHS.index(len(payload)) * 16 + frame["local_bus"] * 2 + int(frame["fd"])
  word = frame["address"] * 8 + int(frame["extended"]) * 4 + int(frame["returned"]) * 2 + int(frame["rejected"])
  prefix = bytes([byte0]) + struct.pack("<I", word)
  checksum = 0
  for value in prefix + payload:
    checksum ^= value
  return prefix + bytes([checksum]) + payload


def main():
  if not __debug__:
    raise RuntimeError("Oracle assertions must be enabled; do not use Python -O")
  parser = argparse.ArgumentParser()
  parser.add_argument("binary", type=Path)
  parser.add_argument("--health-fixture", type=Path)
  args = parser.parse_args()
  binary = str(args.binary.resolve())
  checks = []

  def call(arguments, accepted, expected=None):
    checks.append((arguments, accepted, expected or {}))

  def encode_args(frame, capability, direction):
    return ["--encode", capability, direction, str(frame["address"]), str(frame["local_bus"]),
            *(str(int(frame[k])) for k in ["extended", "fd", "returned", "rejected"]), frame["payload_hex"]]

  rng = random.Random(0x501C0DEC)
  vectors = []
  for length in LENGTHS:
    for extended in [False, True]:
      vectors.append(dict(address=(0x1FFFFFFF if extended else 0x7FF), local_bus=length % 3,
                          extended=extended, fd=length > 8, returned=False, rejected=False,
                          payload_hex=bytes((i * 31 + length) % 256 for i in range(length)).hex()))
  for _ in range(32):
    extended = bool(rng.getrandbits(1))
    length = rng.choice(LENGTHS)
    receipt = rng.choice(["ordinary", "returned", "rejected"])
    vectors.append(dict(address=rng.randrange(0x20000000 if extended else 0x800),
                        local_bus=rng.randrange(3), extended=extended,
                        fd=length > 8 or bool(rng.getrandbits(1)),
                        returned=receipt == "returned", rejected=receipt == "rejected",
                        payload_hex=bytes(rng.randrange(256) for _ in range(length)).hex()))

  for frame in vectors:
    raw = wire(frame)
    call(["--decode", "h7", "rx", raw.hex()], True, frame)
    call(encode_args(frame, "h7", "rx"), True, {"wire_hex": raw.hex()})
    if frame["fd"]:
      call(["--decode", "f4", "rx", raw.hex()], False)
    if frame["returned"] or frame["rejected"]:
      call(["--decode", "h7", "tx", raw.hex()], False)

  example = dict(address=0x123, local_bus=1, extended=False, fd=False,
                 returned=False, rejected=False, payload_hex="112233")
  assert wire(example).hex() == "321809000023112233"
  raw = wire(example)
  if args.health_fixture:
    baseline_fixture = json.loads(args.health_fixture.read_text())
    assert bytes.fromhex(baseline_fixture["can_frame_hex"]) == raw
    call(["--decode", "h7", "rx", baseline_fixture["can_frame_hex"]], True, example)
    fd_example = dict(address=0x18DA10F1, local_bus=2, extended=True, fd=True,
                      returned=True, rejected=False, payload_hex=bytes(range(12)).hex())
    assert bytes.fromhex(baseline_fixture["can_fd_frame_hex"]) == wire(fd_example)
    call(["--decode", "h7", "rx", baseline_fixture["can_fd_frame_hex"]], True, fd_example)
    call(["--decode", "f4", "rx", baseline_fixture["can_fd_frame_hex"]], False)
  for length in range(len(raw)):
    call(["--decode", "h7", "rx", raw[:length].hex()], False)
  for index in range(len(raw)):
    corrupt = bytearray(raw)
    corrupt[index] ^= 0x80
    call(["--decode", "h7", "rx", corrupt.hex()], False)
  for bus in range(3, 8):
    call(["--decode", "h7", "rx", wire({**example, "local_bus": bus}).hex()], False)
  for invalid in [{"returned": True, "rejected": True}, {"address": 0x800}]:
    call(["--decode", "h7", "rx", wire({**example, **invalid}).hex()], False)
  short_fd = wire({**example, "fd": True})
  call(["--decode", "f4", "rx", short_fd.hex()], False)
  batch_input = "\n".join(" ".join(arg or "-" for arg in command) for command, _, _ in checks) + "\n"
  result = subprocess.run([binary, "--batch"], input=batch_input, capture_output=True, text=True, timeout=30, check=True)
  lines = result.stdout.splitlines()
  assert len(lines) == len(checks), (len(lines), len(checks), result.stderr)
  for line, (command, accepted, expected) in zip(lines, checks, strict=True):
    value = json.loads(line)
    assert value["ok"] is accepted, (command, value)
    assert {key: value[key] for key in expected} == expected, (command, value, expected)
  print(json.dumps({"status": "PASS", "oracle": "independent Python bit formula and fixed classic vector",
                    "seed": "0x501C0DEC", "valid_vectors": len(vectors), "cross_language_checks": len(checks), "native_invocations": 1,
                    "baseline_c_layout_vector_checked": args.health_fixture is not None,
                    "assertions_enabled": __debug__,
                    "device_access": False, "scope": "host C++ wire codec only"}))


if __name__ == "__main__":
  main()
