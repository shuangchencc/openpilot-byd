"""Offline, bounded-memory replay of hash-verified Song PLUS rlog caches.

The cache preserves source Event order, raw source IDs and complete CAN payloads.
Only its ``can`` stream with raw source 0 is supplied to the read-only prototype.
Old carState values are comparison labels, never independent vehicle truth.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Iterator

import numpy as np


LAB = Path(__file__).resolve().parents[1]
CANDIDATE_BOOLEAN_FIELDS = ("gasPressed", "brakePressed", "cruiseState.standstill")
RESERVED_UNKNOWN_FIELDS = ("cruiseState.enabled", "cruiseState.available")
BOOLEAN_FIELDS = frozenset((*CANDIDATE_BOOLEAN_FIELDS, *RESERVED_UNKNOWN_FIELDS,
  "leftBlinker", "rightBlinker", "standstill", "steeringPressed", "steerFaultTemporary",
  "steerFaultPermanent", "doorOpen", "seatbeltUnlatched", "leftBlindspot", "rightBlindspot", "espDisabled"))


def sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
      digest.update(block)
  return digest.hexdigest()


def checked_json(path: Path, expected: str | None = None) -> dict:
  raw = path.read_bytes()
  if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
    raise ValueError(f"JSON provenance hash mismatch: {path}")
  return json.loads(raw)


def verify_inputs(analysis_path: Path) -> tuple[dict, dict]:
  """Verify the complete existing extraction chain without loading vendor code.

  This reuses a previously strict decompression/Cap'n Proto validation, after
  rehashing every original rlog, cache and dependency. It does not pretend to
  re-extract the rlog, nor to validate the original schema's vehicle semantics.
  """
  analysis_path = analysis_path.resolve()
  analysis = checked_json(analysis_path)
  if analysis.get("schema") != "song-rlog-first-analysis/v1":
    raise ValueError("Unsupported analysis manifest")
  cache_ref = analysis["files"]["cache/manifest.json"]
  cache_path = Path(cache_ref["path"])
  cache = checked_json(cache_path, cache_ref["sha256"])
  if cache_ref["sha256"] != analysis["cache_manifest_sha256"]:
    raise ValueError("Analysis cache manifest references disagree")
  if cache.get("schema") != "song-local-rlog-cache/v1":
    raise ValueError("Unsupported cache manifest")
  if not cache.get("raw_src_preserved") or not cache.get("event_order_preserved"):
    raise ValueError("Cache must preserve raw src and Event order")
  report_path = Path(cache["source_report"])
  report = checked_json(report_path, cache["source_report_sha256"])
  if report.get("schema") != "c3xl-rlog-content-validation/v1":
    raise ValueError("Unsupported content validation manifest")
  if report.get("status") != "content_valid" or report.get("files_failed") != 0:
    raise ValueError("Source rlogs did not pass strict content validation")
  export_path = Path(report["source_report"])
  export = checked_json(export_path, report["source_report_sha256"])
  if report["route"] != analysis["route"] or export["route"] != analysis["route"]:
    raise ValueError("Route mismatch in provenance chain")
  if report["schema_files_sha256"] != cache["schema_sha256"]:
    raise ValueError("Cache schema differs from strictly validated source")
  for path, expected in cache["schema_sha256"].items():
    if sha256(Path(path)) != expected:
      raise ValueError(f"Source log schema hash mismatch: {path}")
  extractor = LAB / "offline-analysis/extract_route.py"
  if sha256(extractor) != cache["extractor_sha256"]:
    raise ValueError("Cache extractor differs from recorded implementation")
  candidate_ref = analysis["files"]["state-candidates-final.json"]
  checked_json(Path(candidate_ref["path"]), candidate_ref["sha256"])
  entries = cache["segments"]
  count = analysis["input_segments"]
  if len(entries) != count or len(report["files"]) != count or report["files_passed"] != count:
    raise ValueError("Incomplete segment coverage")
  if count == 0:
    raise ValueError("No rlogs to replay")
  if [Path(e["cache_path"]) for e in entries] != sorted(cache_path.parent.glob("segment-*.npz")):
    raise ValueError("Cache directory coverage/order differs from manifest")
  verified = []
  for index, (entry, original) in enumerate(zip(entries, report["files"], strict=True)):
    source, cached = Path(entry["source_path"]), Path(entry["cache_path"])
    if source.parent.name != f"{analysis['route']}--{index}" or cached.name != f"segment-{index:03d}.npz":
      raise ValueError("Segment indices must be continuous from zero and in original order")
    if str(source) != original["path"] or entry["source_sha256"] != original["sha256"]:
      raise ValueError(f"Cache source differs from validated rlog: segment {index}")
    if entry["source_events"] != original["events"] or original["status"] != "content_valid":
      raise ValueError(f"Invalid source event count/status: segment {index}")
    if source.stat().st_size != original["bytes"] or sha256(source) != entry["source_sha256"]:
      raise ValueError(f"Original rlog hash/size mismatch: segment {index}")
    if sha256(cached) != entry["cache_sha256"]:
      raise ValueError(f"Cache hash mismatch: segment {index}")
    # The existing per-frame cache cannot represent an empty CAN Event. Retain
    # the rlog validator's Event count so replay must reject that coverage loss.
    entry["original_can_events"] = original["event_types"].get("can", 0)
    verified.append({"segment": index, "rlog_sha256": entry["source_sha256"],
                     "cache_sha256": entry["cache_sha256"], "rlog_bytes": original["bytes"],
                     "original_can_events": entry["original_can_events"]})
  return cache, {
    "analysis_manifest": str(analysis_path), "analysis_manifest_sha256": sha256(analysis_path),
    "cache_manifest": str(cache_path), "cache_manifest_sha256": sha256(cache_path),
    "source_content_validation": str(report_path), "source_content_validation_sha256": sha256(report_path),
    "export_report_sha256": sha256(export_path), "extractor_sha256": cache["extractor_sha256"],
    "schema_sha256": cache["schema_sha256"], "route": analysis["route"],
    "rlog_hashes_verified": count, "cache_hashes_verified": count,
    "bytes_rehashed": sum(e["rlog_bytes"] for e in verified), "segments": verified,
    "method": "Rehashed original rlogs and exact previously extracted caches; strict content validation reused by hash, not rerun.",
    "reference_caveat": "Old decoded carState and fitted wheel scale are not independent physical ground truth.",
  }


def verify_candidate_research(provenance: dict, manifest_path: Path, historical_replay_source: Path) -> dict:
  """Recheck fixed historical evidence, without executing its research scripts.

  The old verifier is checked against its preserved source, never against this
  newly extended verifier. Only evidence inputs are checked: historical README
  hashes must not prevent a later documented implementation update.
  """
  manifest_path = manifest_path.resolve()
  manifest = checked_json(manifest_path)
  if manifest.get("result") != "pass":
    raise ValueError("Candidate research verification did not pass")
  old_verifier_hash = manifest["runtime_source_sha256"]["songplus_readonly/replay.py"]
  if sha256(historical_replay_source) != old_verifier_hash:
    raise ValueError("Historical replay source does not match research verification")
  base = "results/songplus-pressed-acc-20261018/"
  archived_screen = base + "cruise-bit-screen-source-before-metadata-correction.py"
  sources = {
    "pedal-pressed.json": "offline-analysis/pedal_pressed.py",
    "brake-bit-screen.json": "offline-analysis/brake_bit_screen.py",
    "cruise-bit-screen.json": archived_screen,
    "cruise-bit-holdout.json": archived_screen,
  }
  checked = {}

  def check_file(relative):
    path = LAB / relative
    expected = manifest["file_sha256"][relative]
    if sha256(path) != expected:
      raise ValueError(f"Historical candidate evidence hash mismatch: {relative}")
    checked[relative] = expected
    return path

  reports = {}
  chain_keys = ("analysis_manifest_sha256", "cache_manifest_sha256", "source_content_validation_sha256",
                "export_report_sha256", "extractor_sha256", "schema_sha256", "route", "segments")
  for report_name, source in sources.items():
    report = checked_json(check_file(base + report_name))
    source_path = check_file(source)
    if sha256(source_path) != report["script_sha256"]:
      raise ValueError(f"Historical report script mismatch: {report_name}")
    for key in chain_keys:
      if report["provenance"][key] != provenance[key]:
        raise ValueError(f"Candidate evidence input chain differs at {key}: {report_name}")
    reports[report_name] = report
  correction = checked_json(check_file(base + "cruise-bit-screen-metadata-correction.json"))
  if correction["original_source"]["sha256"] != checked[archived_screen]:
    raise ValueError("Metadata correction original source mismatch")
  for reference in correction["reports"]:
    relative = base + Path(reference["path"]).name
    if reference["sha256"] != checked[relative]:
      raise ValueError("Metadata correction references different numerical report")
  if reports["cruise-bit-screen.json"]["segment_partition"] != [0, 24] or reports["cruise-bit-holdout.json"]["segment_partition"] != [25, 97]:
    raise ValueError("Cruise candidate discovery/validation partitions changed")
  if not {"gas_raw_gt_3", "extra_bit_123_43_0"}.issubset(reports["pedal-pressed.json"]["rules"]):
    raise ValueError("Pressed candidate rule evidence missing")
  if not any(row["target"] == "cruiseState.standstill" and row["raw_src"] == 0
             and row["address"] == "0x220" and row["little_endian_bit_index"] == 8
             and not row["inverted"] for row in reports["cruise-bit-holdout.json"]["fixed_candidates"]):
    raise ValueError("Cruise standstill fixed candidate evidence missing")
  return {"verified": True, "manifest": str(manifest_path), "manifest_sha256": sha256(manifest_path),
    "file_sha256": checked, "historical_replay_source": str(historical_replay_source.resolve()),
    "historical_replay_source_sha256": old_verifier_hash, "input_chain_matches_current_route_and_caches": True,
    "candidate_rules": {"gasPressed": "src0 0x342 byte0>3; historical discovery0, validation1..97",
      "brakePressed": "src0 0x123 little-endian bit43; historical discovery0..2, validation3..97",
      "cruiseState.standstill": "src0 0x220 little-endian bit8; historical discovery0..24, validation25..97"},
    "limitation": "Historical selection/holdout provenance only. This whole-route implementation replay reuses those labels, is not new independent validation, and does not establish physical semantics."}


@dataclass(frozen=True)
class CanEvent:
  segment: int
  event_index: int
  t_ns: int
  valid: bool
  frames: list[tuple[int, bytes, int]]


@dataclass(frozen=True)
class OldState:
  segment: int
  event_index: int
  t_ns: int
  values: dict


def ordered_can_events(cache: dict, counts: Counter, coverage: list | None = None) -> Iterator[CanEvent]:
  """At most one segment's CAN arrays are resident; no source-ID normalization."""
  last_time = -1
  for segment, entry in enumerate(cache["segments"]):
    with np.load(entry["cache_path"], allow_pickle=False) as archive:
      rows, payloads = archive["can"], archive["can_data"]
    if len(rows) != entry["counts"]["can"] or len(payloads) != len(rows):
      raise ValueError("Cache CAN count mismatch")
    if payloads.dtype != np.uint8 or payloads.ndim != 2 or payloads.shape[1] != 64:
      raise ValueError("Unexpected cached CAN payload layout")
    if np.any(rows["event_index"][1:] < rows["event_index"][:-1]):
      raise ValueError("CAN Event order regressed")
    boundaries = (np.r_[0, np.flatnonzero(rows["event_index"][1:] != rows["event_index"][:-1]) + 1, len(rows)]
                  if len(rows) else np.array([0], dtype=int))
    original_events = entry["original_can_events"]
    grouped_events = len(boundaries) - 1
    if grouped_events != original_events:
      raise ValueError(f"CAN Event coverage mismatch in segment {segment}: original={original_events}, "
                       f"cached_groups={grouped_events}; empty or missing Events require a new extraction")
    if coverage is not None:
      coverage.append({"segment": segment, "original_can_events": original_events,
                       "cached_can_event_groups": grouped_events, "omitted_can_events": 0})
    for raw_src, n in zip(*np.unique(rows["src"], return_counts=True), strict=True):
      counts[f"raw_src_{int(raw_src)}"] += int(n)
    counts["sendcan_frames_excluded"] += entry["counts"]["sendcan"]
    for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
      group = rows[start:end]
      if not len(group):
        continue
      timestamp, valid = int(group[0]["t_ns"]), bool(group[0]["valid"])
      if timestamp < last_time:
        raise ValueError("CAN timestamps regressed; refuse silently sorting")
      last_time = timestamp
      if np.any(group["t_ns"] != timestamp) or np.any(group["valid"] != valid):
        raise ValueError("One cached CAN Event has inconsistent time/validity")
      if np.any(group["length"] > 64):
        raise ValueError("CAN payload length exceeds cached bytes")
      frames = [(int(row["address"]), bytes(payloads[start + offset, :int(row["length"])]), 0)
                for offset, row in enumerate(group) if int(row["src"]) == 0]
      counts["original_can_events"] += 1
      counts["selected_raw_src_0_frames"] += len(frames)
      counts["invalid_event_selected_frames"] += len(frames) if not valid else 0
      yield CanEvent(segment, int(group[0]["event_index"]), timestamp, valid, frames)


def ordered_old_states(cache: dict) -> Iterator[OldState]:
  last_time = -1
  for segment, entry in enumerate(cache["segments"]):
    with np.load(entry["cache_path"], allow_pickle=False) as archive:
      rows = archive["carState"]
    if len(rows) != entry["counts"]["carState"]:
      raise ValueError("Cache carState count mismatch")
    if np.any(rows["event_index"][1:] <= rows["event_index"][:-1]):
      raise ValueError("carState Event order regressed or repeated")
    for row in rows:
      timestamp = int(row["t_ns"])
      if timestamp < last_time:
        raise ValueError("carState timestamps regressed; refuse silently sorting")
      last_time = timestamp
      values = {name: row[name].item() for name in rows.dtype.names}
      yield OldState(segment, int(row["event_index"]), timestamp, values)


def causally_available(can: CanEvent, state: OldState) -> bool:
  # Both conditions matter: logger publication order can differ from producer
  # timestamps. Neither a later record nor a future timestamp may be consumed.
  return ((can.segment, can.event_index) < (state.segment, state.event_index)
          and can.t_ns <= state.t_ns)


@dataclass
class Comparison:
  tolerance: float
  binary: bool = False
  observations: int = 0
  old_invalid: int = 0
  old_missing: int = 0
  old_not_boolean: int = 0
  valid_reference_samples: int = 0
  prototype_unavailable: int = 0
  prototype_unavailable_with_valid_reference: int = 0
  prototype_unavailable_reasons: Counter = field(default_factory=Counter)
  quality_counts: Counter = field(default_factory=Counter)
  confusion: Counter = field(default_factory=Counter)
  compared: int = 0
  equal: int = 0
  absolute_error_sum: float = 0.0
  maximum_absolute_error: float = 0.0
  minimum: float | None = None
  maximum: float | None = None
  mismatch_examples: list = field(default_factory=list)

  def observe(self, value, reference, *, old_valid: bool, reason: str, location: tuple) -> None:
    self.observations += 1
    self.quality_counts[reason] += 1
    if value is None:
      self.prototype_unavailable += 1
      self.prototype_unavailable_reasons[reason] += 1
    if not old_valid:
      self.old_invalid += 1
      return
    if reference is None or isinstance(reference, (int, float)) and not math.isfinite(reference):
      self.old_missing += 1
      return
    if self.binary and reference not in (False, True):
      self.old_not_boolean += 1
      return
    self.valid_reference_samples += 1
    if value is None:
      self.prototype_unavailable_with_valid_reference += 1
      return
    if self.binary:
      if not isinstance(value, bool):
        raise ValueError("Prototype boolean candidate must be bool or None")
      self.confusion[("T" if value == bool(reference) else "F") + ("P" if value else "N")] += 1
    self.compared += 1
    if isinstance(value, (int, float)) and isinstance(reference, (int, float)):
      if not math.isfinite(value):
        raise ValueError("Prototype returned nonfinite value instead of None")
      error = abs(float(value) - float(reference))
      self.minimum = float(value) if self.minimum is None else min(self.minimum, float(value))
      self.maximum = float(value) if self.maximum is None else max(self.maximum, float(value))
    else:
      error = float(value != reference)
    self.absolute_error_sum += error
    self.maximum_absolute_error = max(self.maximum_absolute_error, error)
    equal = error <= self.tolerance
    self.equal += int(equal)
    if not equal and len(self.mismatch_examples) < 10:
      self.mismatch_examples.append({"segment": location[0], "event_index": location[1],
                                     "t_ns": location[2], "prototype": value, "old_carState": reference})

  def result(self) -> dict:
    result = {
      "observations": self.observations, "old_invalid": self.old_invalid, "old_missing": self.old_missing,
      "old_not_boolean": self.old_not_boolean, "valid_reference_samples": self.valid_reference_samples,
      "prototype_unavailable": self.prototype_unavailable,
      "prototype_unavailable_with_valid_reference": self.prototype_unavailable_with_valid_reference,
      "prototype_unavailable_reasons": dict(self.prototype_unavailable_reasons),
      "quality_counts": dict(self.quality_counts),
      "compared": self.compared, "equal_within_tolerance": self.equal, "tolerance": self.tolerance,
      "agreement_fraction": self.equal / self.compared if self.compared else None,
      "mean_absolute_error": self.absolute_error_sum / self.compared if self.compared else None,
      "maximum_absolute_error": self.maximum_absolute_error if self.compared else None,
      "prototype_range": [self.minimum, self.maximum], "mismatch_examples": self.mismatch_examples,
    }
    if self.binary:
      result["binary_confusion"] = {key: self.confusion[key] for key in ("TN", "FP", "FN", "TP")}
      if sum(result["binary_confusion"].values()) != self.compared:
        raise ValueError("Boolean confusion counts do not cover compared observations")
    return result


def assert_reserved_unknowns(snapshot) -> None:
  for name in RESERVED_UNKNOWN_FIELDS:
    if name not in snapshot.values or snapshot.values[name] is not None or snapshot.quality.get(name) != "unconfirmed":
      raise ValueError(f"Reserved ACC field must remain explicitly unconfirmed/None: {name}")


def check_projection(snapshot) -> dict:
  """Check candidate projection without promoting schema defaults to observations."""
  assert_reserved_unknowns(snapshot)
  projection = snapshot.to_opendbc()
  if projection.canValid:
    raise ValueError("Read-only research projection must not advertise canValid")
  compared = {}
  for name in CANDIDATE_BOOLEAN_FIELDS:
    if name not in snapshot.values:
      raise ValueError(f"Prototype omitted candidate field: {name}")
    expected = snapshot.values[name]
    node = projection
    for component in name.split("."):
      node = getattr(node, component)
    if expected is not None and (not isinstance(expected, bool) or bool(node) != expected):
      raise ValueError(f"Official projection differs from candidate sidecar: {name}")
    compared[name] = {"sidecar": expected, "projected": bool(node), "usable": expected is not None}
  return {"canValid": False, "candidate_fields": compared}


def runtime_source_hashes() -> dict[str, str]:
  root = LAB / "songplus_readonly"
  return {str(path.relative_to(LAB)): sha256(path) for path in sorted(root.rglob("*"))
          if path.is_file() and path.suffix in (".py", ".dbc") and "tests" not in path.relative_to(root).parts}


def replay(cache: dict, provenance: dict, candidate_research: dict | None = None) -> dict:
  runtime_hashes_at_start = runtime_source_hashes()
  # The independent decoder imports official opendbc only. No installed private
  # car interface, controller, Panda, manager or device runtime is loaded.
  from songplus_readonly.carstate import FIELD_EVIDENCE, SongPlusReadonlyCarState

  decoder = SongPlusReadonlyCarState()
  counts: Counter = Counter()
  can_coverage = []
  can_events = iter(ordered_can_events(cache, counts, can_coverage))
  pending = next(can_events, None)
  comparisons = {name: Comparison(0 if name in BOOLEAN_FIELDS else 1e-4 if name == "steeringAngleDeg" else 1e-5,
                                  binary=name in BOOLEAN_FIELDS)
                 for name in FIELD_EVIDENCE}
  segment_results = []
  current_segment = -1
  current_counts: Counter = Counter()
  last_can_t = -1
  last_state_t = -1
  last_snapshot = None
  last_projection_signature = None
  projection_samples = 0
  projection_candidate_values = {name: Counter() for name in CANDIDATE_BOOLEAN_FIELDS}
  for old in ordered_old_states(cache):
    first_state_in_segment = old.segment != current_segment
    if old.segment != current_segment:
      if current_segment >= 0:
        segment_results.append({"segment": current_segment, **dict(current_counts)})
        print(f"Replayed segment {current_segment:03d}: {current_counts['old_carState']} old states", flush=True)
      current_segment = old.segment
      current_counts = Counter()
    counts["old_carState"] += 1
    current_counts["old_carState"] += 1
    old_valid = bool(old.values["valid"]) and old.values.get("canValid") == 1.0
    counts["old_valid_and_canValid"] += int(old_valid)
    while pending is not None and causally_available(pending, old):
      decoder.ingest_batch(pending.t_ns, pending.frames, event_valid=pending.valid)
      counts["can_events_fed"] += 1
      last_can_t = pending.t_ns
      pending = next(can_events, None)
    if pending is not None:
      if ((pending.segment, pending.event_index) < (old.segment, old.event_index)
          and pending.t_ns > old.t_ns):
        counts["states_with_file_earlier_future_timestamp_can_deferred"] += 1
      elif pending.t_ns <= old.t_ns:
        counts["states_with_timestamp_earlier_later_file_can_deferred"] += 1
    snapshot = decoder.snapshot(old.t_ns)
    if snapshot.timestamp_ns != old.t_ns:
      raise ValueError("Prototype snapshot timestamp changed")
    assert_reserved_unknowns(snapshot)
    counts["snapshots_checked_reserved_acc_unknown"] += 1
    signature = tuple((snapshot.values[name], snapshot.quality[name]) for name in CANDIDATE_BOOLEAN_FIELDS)
    if first_state_in_segment or signature != last_projection_signature:
      check_projection(snapshot)
      projection_samples += 1
      for name in CANDIDATE_BOOLEAN_FIELDS:
        projection_candidate_values[name][str(snapshot.values[name])] += 1
      last_projection_signature = signature
    last_snapshot, last_state_t = snapshot, old.t_ns
    for name, comparison in comparisons.items():
      if name not in snapshot.values or name not in snapshot.quality:
        raise ValueError(f"Prototype omitted nullable field/quality: {name}")
      comparison.observe(snapshot.values[name], old.values.get(name), old_valid=old_valid,
                         reason=snapshot.quality[name], location=(old.segment, old.event_index, old.t_ns))
  if current_segment >= 0:
    segment_results.append({"segment": current_segment, **dict(current_counts)})
    print(f"Replayed segment {current_segment:03d}: {current_counts['old_carState']} old states", flush=True)
  # Preserve the entire CAN stream, including the final records after the final
  # old carState. They must not be silently dropped just to improve agreement.
  while pending is not None:
    decoder.ingest_batch(pending.t_ns, pending.frames, event_valid=pending.valid)
    counts["can_events_fed"] += 1
    counts["can_events_after_final_old_state"] += 1
    last_can_t = pending.t_ns
    pending = next(can_events, None)
  if last_can_t < 0 or last_snapshot is None:
    raise ValueError("Replay requires CAN and old carState observations")
  final_snapshot = decoder.snapshot(max(last_can_t, last_state_t))
  final_projection = check_projection(final_snapshot)
  projection_samples += 1
  counts["snapshots_checked_reserved_acc_unknown"] += 1
  for name in CANDIDATE_BOOLEAN_FIELDS:
    projection_candidate_values[name][str(final_snapshot.values[name])] += 1
  runtime_hashes_at_end = runtime_source_hashes()
  if runtime_hashes_at_start != runtime_hashes_at_end:
    raise ValueError("Prototype runtime source changed during replay; refuse a misleading implementation hash")
  files = sorted(p for p in (LAB / "songplus_readonly").rglob("*")
                 if p.is_file() and p.suffix in (".py", ".dbc", ".json"))
  return {
    "schema": "songplus-readonly-carstate-replay/v1", "status": "completed_offline_replay",
    "created_at_utc": datetime.now(timezone.utc).isoformat(), "provenance": provenance,
    "candidate_research": candidate_research or {"verified": False, "reason": "No explicit historical research manifest supplied"},
    "ordering": {
      "can_order": "Original segment order, Event index, within-Event frame order; no sorting/interpolation",
      "matching": "Consume only CAN preceding the old state in both file order and logMonoTime; carry pending CAN across segments",
      "source_filter": "Only kind=can and raw src exactly 0; sendcan and all other raw src values excluded",
      "clock": "Raw monotonic nanoseconds; no wall-clock date or speed calibration inferred",
      "memory": "One CAN segment and one carState segment plus bounded summary statistics",
      "reference_eligibility": "Old Event.valid and old canValid==1; finite field value; booleans must be exactly0/1. Unknown prototype values excluded, never coerced to false.",
    },
    "counts": dict(counts), "segments": segment_results, "can_event_coverage": can_coverage,
    "fields": {name: {"evidence": FIELD_EVIDENCE[name], **comparison.result()}
               for name, comparison in comparisons.items()},
    "decoder_diagnostics": decoder.diagnostics(),
    "final_snapshot": {"timestamp_ns": final_snapshot.timestamp_ns,
                       "values": final_snapshot.values, "quality": final_snapshot.quality},
    "reserved_acc_unknown": {"fields": list(RESERVED_UNKNOWN_FIELDS), "all_checked_values_none": True,
                             "quality_always_unconfirmed": True,
                             "snapshots_checked": counts["snapshots_checked_reserved_acc_unknown"]},
    "official_projection": {"created": True, "canValid": False,
                            "samples_checked": projection_samples,
                            "sampling": "First old state per segment, each new candidate value/quality change, and final snapshot after all remaining CAN",
                            "candidate_values_checked": {name: dict(values) for name, values in projection_candidate_values.items()},
                            "final_candidate_projection": final_projection["candidate_fields"],
                            "unknown_defaults_usable": False,
                            "nullable_sidecar_required": True},
    "implementation_sha256": {str(p.relative_to(LAB)): sha256(p) for p in files},
    "runtime_source_sha256": runtime_hashes_at_start, "runtime_source_unchanged_during_replay": True,
    "vehicle_semantics_independently_confirmed": False, "actuation_allowed": False,
    "network_or_device_access": False, "panda_safety_validated": False,
    "interpretation": "Agreement is reproduction of historical decoder labels, not a new independent validation set or vehicle safety validation. Missing/unconfirmed fields remain null regardless of old false/zero labels.",
  }


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--analysis-manifest", type=Path, required=True)
  parser.add_argument("--candidate-research-manifest", type=Path,
                      help="Optional historical research verification manifest; checked only by this offline runner")
  parser.add_argument("--historical-replay-source", type=Path,
                      help="Preserved old replay.py matching the historical research manifest")
  parser.add_argument("--out", type=Path, required=True, help="New JSON report; existing files are never overwritten")
  args = parser.parse_args()
  if args.out.exists():
    raise FileExistsError(args.out)
  if bool(args.candidate_research_manifest) != bool(args.historical_replay_source):
    parser.error("--candidate-research-manifest and --historical-replay-source must be supplied together")
  cache, provenance = verify_inputs(args.analysis_manifest)
  print(f"Verified {provenance['rlog_hashes_verified']} original rlogs and {provenance['cache_hashes_verified']} cache hashes", flush=True)
  research = (verify_candidate_research(provenance, args.candidate_research_manifest, args.historical_replay_source)
              if args.candidate_research_manifest else None)
  result = replay(cache, provenance, research)
  args.out.parent.mkdir(parents=True, exist_ok=True)
  # Exclusive creation also protects against another replay finishing first.
  with args.out.open("x") as handle:
    json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    handle.write("\n")
  print(f"Completed read-only replay: {args.out.resolve()}", flush=True)


if __name__ == "__main__":
  main()
