"""Decode recorded bytes using a pinned reference, without asserting Song compatibility."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from replay.canlog import Frame, ValidationError, load_recording, parse_frame

ROOT = Path(__file__).resolve().parents[1]
PROFILES = Path(__file__).with_name("profiles.json")


class ReferenceError(ValueError):
  pass


def load_reference(profile_id: str):
  config = json.loads(PROFILES.read_text())
  if profile_id not in config["profiles"]:
    raise ReferenceError(f"Unknown reference profile: {profile_id}")
  profile = config["profiles"][profile_id]
  upstream = ROOT / config["upstream_directory"]
  revision = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
  if revision != config["upstream_commit"]:
    raise ReferenceError("The upstream checkout does not match the pinned commit")
  dirty = subprocess.run(["git", "-C", str(upstream), "diff", "--quiet", "HEAD", "--"], check=False)
  if dirty.returncode != 0:
    raise ReferenceError("The upstream checkout has tracked changes; re-audit before decoding")
  dbc = upstream / profile["dbc_path"]
  if hashlib.sha256(dbc.read_bytes()).hexdigest() != profile["dbc_sha256"]:
    raise ReferenceError("Reference DBC hash does not match the profile")

  # Import only the pinned host-side parser, never panda, manager or a controller instance.
  import opendbc
  from opendbc.can.parser import CANParser
  if Path(opendbc.__file__).resolve().parent != upstream / "opendbc":
    raise ReferenceError("Python resolved a different opendbc; use the lab's isolated environment")
  return profile, dbc, CANParser


def decode_frames(frames: tuple[Frame, ...], *, profile_id: str, bus: int, data_kind: str) -> dict:
  if type(bus) is not int or not 0 <= bus <= 255:
    raise ReferenceError("An explicit bus in 0..255 is required; physical topology is not inferred")
  if data_kind not in {"synthetic", "recorded"}:
    raise ReferenceError("Unknown data provenance")
  profile, dbc, parser_class = load_reference(profile_id)
  # Python callers must satisfy the same frame contract as the JSONL entry point.
  # Materialize before decoding so a late invalid frame never yields partial output.
  frames = tuple(frames)
  if not frames:
    raise ValidationError("Cannot decode an empty recording")
  previous_timestamp = -1
  for index, frame in enumerate(frames, 1):
    if not isinstance(frame, Frame) or not isinstance(frame.data, bytes):
      raise ValidationError(f"frame {index}: expected Frame with bytes data")
    parse_frame({"timestamp_ns": frame.timestamp_ns, "bus": frame.bus,
                 "address": frame.address, "data": frame.data.hex()}, index)
    if frame.timestamp_ns < previous_timestamp:
      raise ValidationError(f"frame {index}: timestamps must be globally nondecreasing")
    previous_timestamp = frame.timestamp_ns
  parser = parser_class(str(dbc), [], bus)
  events = []
  counts = Counter()
  for frame in frames:
    event = {"timestamp_ns": frame.timestamp_ns, "bus": frame.bus, "address": frame.address,
             "data": frame.data.hex(), "signals": None}
    if frame.bus != bus:
      event["status"] = "different_bus"
    elif frame.address not in parser.dbc.addr_to_msg:
      event["status"] = "not_in_reference_dbc"
    else:
      message = parser.dbc.addr_to_msg[frame.address]
      event["reference_message"] = message.name
      event["expected_length"] = message.size
      if len(frame.data) != message.size:
        # Upstream signal extraction can tolerate short data; the lab must not zero-fill evidence.
        event["status"] = "length_mismatch"
      else:
        if frame.address not in parser.addresses:
          parser._add_message(frame.address)
        updated = parser.update([frame.timestamp_ns, [(frame.address, frame.data, frame.bus)]])
        state = parser.message_states[frame.address]
        event["checksum_fields_checked"] = [s.name for s in state.signals if s.calc_checksum is not None]
        event["counter_fields_checked"] = [s.name for s in state.signals if s.type == 1]
        event["integrity_fields_without_validator"] = [s.name for s in state.signals
          if ("checksum" in s.name.lower() or "crc" in s.name.lower()) and s.calc_checksum is None]
        event["counter_fail_count"] = state.counter_fail
        if frame.address in updated:
          event["status"] = "decoded_candidate"
          event["signals"] = dict(parser.vl[frame.address])
        else:
          event["status"] = "rejected_by_reference_parser"
          # Never publish stale parser.vl values as if they came from this rejected frame.
    counts[event["status"]] += 1
    events.append(event)
  return {
    "schema": "candidate-decode/v1", "profile": profile_id, "reference": profile,
    "data_kind": data_kind, "selected_logical_bus": bus,
    "vehicle_compatibility": "unverified", "actuation_allowed": False,
    "interpretation": "DBC labels are hypotheses from another vehicle. Parsing is not vehicle-state validation.",
    "limitations": ["No CarState or control messages are produced.",
                    "The selected bus is a recording index, not a verified physical channel.",
                    "Per-message counter tolerance follows the pinned parser; it is not proof of signal correctness.",
                    "No frequency/timeout or safety-mode readiness is inferred from successful decoding."],
    "counts": dict(counts), "events": events,
  }


def main(argv=None) -> int:
  argparser = argparse.ArgumentParser(description=__doc__)
  argparser.add_argument("manifest", type=Path)
  argparser.add_argument("--profile", required=True)
  argparser.add_argument("--bus", required=True, type=int)
  args = argparser.parse_args(argv)
  try:
    manifest, frames = load_recording(args.manifest)
    result = decode_frames(frames, profile_id=args.profile, bus=args.bus, data_kind=manifest["data_kind"])
    result["input_sha256"] = manifest["sha256"]
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
  except (ValidationError, ReferenceError, OSError, ValueError, subprocess.SubprocessError, ImportError) as error:
    print(json.dumps({"error": str(error), "actuation_allowed": False}, ensure_ascii=False), file=sys.stderr)
    return 2
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
