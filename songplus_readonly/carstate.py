"""Read-only candidate CarState using the pinned official CANParser.

Snapshot.values is authoritative: None means unknown or unavailable. The
optional official Cap'n Proto projection cannot represent nullable primitives;
it always has canValid=False and must travel with this evidence sidecar.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from songplus_readonly.runtime import verify_runtime

RUNTIME = verify_runtime()

from opendbc.can.parser import CANParser  # noqa: E402
from opendbc.car import structs  # noqa: E402

DBC_PATH = Path(__file__).parent / "dbc/byd_song_plus_readonly.dbc"
MESSAGES = {0x11F: (5, 100), 0x123: (8, 100), 0x133: (8, 20), 0x1F0: (8, 50),
            0x1FC: (8, 50), 0x220: (8, 50), 0x242: (8, 50), 0x342: (8, 50)}
SIGNALS = {
  "steeringAngleDeg": (0x11F, "ANGLE"), "steeringTorqueEps": (0x1FC, "EPS_NATIVE"),
  "wheelSpeeds.fl": (0x1F0, "FL"), "wheelSpeeds.fr": (0x1F0, "FR"),
  "wheelSpeeds.rl": (0x1F0, "RL"), "wheelSpeeds.rr": (0x1F0, "RR"),
  "leftBlinker": (0x133, "LEFT"), "rightBlinker": (0x133, "RIGHT"),
  "gearShifter": (0x242, "GEAR_RAW"), "gas": (0x342, "GAS"), "brake": (0x342, "BRAKE"),
  "gasPressed": (0x342, "GAS_RAW"), "brakePressed": (0x123, "BRAKE_PRESSED_CANDIDATE"),
  "cruiseState.standstill": (0x220, "CRUISE_STANDSTILL_CANDIDATE"),
}


def evidence(tier: str, note: str, address: int | None = None,
             source: str = "results/rlog-analysis-20261018/state-candidates-final.json") -> dict:
  return {"tier": tier, "vehicle_confirmed": False, "note": note,
          "candidate": tier == "empirical_candidate",
          "raw_src": 0 if address is not None else None,
          "address": f"0x{address:X}" if address is not None else None,
          "source": source}


FIELD_EVIDENCE = {
  "steeringAngleDeg": evidence("supported_encoding", "Signed16 x0.1 reproduces historical angle; sign/zero require physical annotation. 4-bit checksum unresolved.", 0x11F),
  "steeringTorqueEps": evidence("supported_encoding", "Signed12 reproduces historical EPS torque; native units, not proven Nm or driver torque.", 0x1FC),
  "brake": evidence("supported_encoding", "Byte1 x0.01 reproduces historical normalized value; not brakePressed or pressure calibration.", 0x342),
  "gas": evidence("empirical_candidate", "Byte0 x0.01 retained directly. Historical 1.45% residual is explained by truncation; physical scale remains unverified.", 0x342,
                  "results/songplus-pedal-gear-20261018/pedal-residuals.json"),
  "gearShifter": evidence("empirical_candidate", "Historical labels 1=P, 2=R, 4=D; parking R context is consistent with revised recollection. N/physical direction/checksum/counter unconfirmed.", 0x242,
                          "results/songplus-pedal-gear-20261018/gear-context.json"),
  "gasPressed": evidence("empirical_candidate", "Raw byte0 >3 reproduces historical labels; not a calibrated physical pedal threshold. Simultaneous pedal/override semantics unobserved.", 0x342,
                         "results/songplus-pressed-acc-20261018/pedal-pressed.json"),
  "brakePressed": evidence("empirical_candidate", "Bit43 reproduces historical labels independently of brake amount. Driver braking versus automatic braking/hold unconfirmed; checksum/counter are observational candidates.", 0x123,
                           "results/songplus-pressed-acc-20261018/pedal-pressed.json"),
  "cruiseState.standstill": evidence("empirical_candidate", "Bit8 reproduces historical cruise standstill, not equivalent to vehicle standstill. Actual ACC hold semantics and checksum/counter unknown.", 0x220,
                                    "results/songplus-pressed-acc-20261018/cruise-bit-holdout.json"),
  "leftBlinker": evidence("supported_encoding", "Bit37 reproduces historical leftBlinker; no independent annotated lamp check; checksum unknown.", 0x133),
  "rightBlinker": evidence("supported_encoding", "Bit38 reproduces historical rightBlinker; no independent annotated lamp check; checksum unknown.", 0x133),
  **{f"wheelSpeeds.{wheel}": evidence("empirical_candidate", "12-bit candidate x0.0725 km/h fitted to old decoder; physical wheel identity/width/scale unverified.", 0x1F0)
     for wheel in ("fl", "fr", "rl", "rr")},
  "vEgoRaw": evidence("empirical_candidate", "Mean of four wheel candidates; no independent GPS/speed reference.", 0x1F0),
  **{name: evidence("unconfirmed", "No sufficiently supported independent mapping; deliberately None, including when old decoder reports false/zero.")
     for name in ("vEgo", "aEgo", "vEgoCluster", "standstill", "steeringRateDeg", "steeringTorque",
                  "steeringPressed", "steerFaultTemporary", "steerFaultPermanent",
                  "cruiseState.enabled", "cruiseState.available", "cruiseState.speed",
                  "doorOpen", "seatbeltUnlatched", "leftBlindspot", "rightBlindspot", "espDisabled")},
}


@dataclass(frozen=True)
class Snapshot:
  timestamp_ns: int
  values: dict
  quality: dict[str, str]
  age_ns: dict[str, int | None]

  def to_opendbc(self):
    """Lossy research projection; None is NOT a physical zero/false reading."""
    ret = structs.CarState()
    ret.canValid = False
    for name, value in self.values.items():
      if value is None or name in ("gas", "brake"):
        continue
      if name.startswith("wheelSpeeds."):
        setattr(ret.wheelSpeeds, name.split(".")[1], value)
      elif name.startswith("cruiseState."):
        setattr(ret.cruiseState, name.split(".")[1], value)
      else:
        setattr(ret, name, value)
    # gas/brake only exist in a deprecated group in the target schema. Keep
    # these research observations in the sidecar, not that legacy API.
    return ret


class SongPlusReadonlyCarState:
  """Consumes receive frames only, with no interface/controller/device API."""

  def __init__(self):
    self.parser = CANParser(str(DBC_PATH.resolve()), [(a, hz) for a, (_, hz) in MESSAGES.items()], 0)
    self._last_can_ns = -1
    self._last_snapshot_ns = -1
    self._last_accepted: dict[int, int] = {}
    self._quality = {a: "unseen" for a in MESSAGES}
    self._counts: Counter = Counter()
    self._message_counts = {a: Counter() for a in MESSAGES}
    self._counter_seen: set[int] = set()

  def ingest_batch(self, t_ns: int, frames: list[tuple[int, bytes, int]], event_valid: bool = True) -> None:
    if not isinstance(t_ns, int) or t_ns < 0 or t_ns < self._last_can_ns:
      raise ValueError("CAN timestamps must be nonnegative integer nanoseconds and nondecreasing")
    self._last_can_ns = t_ns
    self._counts["batches"] += 1
    # Each chunk contains at most one instance of an address: the official
    # update() set then unambiguously identifies acceptance of that frame.
    chunk: dict[int, bytes] = {}
    for address, data, src in frames:
      self._counts["frames_input"] += 1
      if src != 0:
        self._counts["ignored_other_raw_src"] += 1
        continue
      if address not in MESSAGES:
        self._counts["ignored_other_address"] += 1
        continue
      if address in chunk:
        self._flush(t_ns, chunk, event_valid)
        chunk = {}
      chunk[address] = data
    self._flush(t_ns, chunk, event_valid)

  def _flush(self, t_ns: int, chunk: dict[int, bytes], event_valid: bool) -> None:
    eligible = []
    rejected = {}
    for address, data in chunk.items():
      stats = self._message_counts[address]
      stats["observed"] += 1
      if not event_valid:
        rejected[address] = "invalid_event"
        continue
      if len(data) != MESSAGES[address][0]:
        rejected[address] = "bad_length"
        continue
      eligible.append((address, data, 0))
      state = self.parser.message_states[address]
      for sig in state.signals:
        if sig.calc_checksum is not None:
          stats["checksum_checked"] += 1
          if sig.get_raw_value(data) != sig.calc_checksum(address, sig, bytearray(data)):
            rejected[address] = "bad_checksum"
        if sig.name == "COUNTER":
          raw = sig.get_raw_value(data)
          if address in self._counter_seen and raw != (state.counter + 1) % (1 << sig.size):
            stats["counter_discontinuities"] += 1
          self._counter_seen.add(address)
    # Bad checksums are also passed to the official parser: its checksum and
    # counter tolerance decide acceptance. Length/invalid Event frames are not.
    updated = self.parser.update([(t_ns, eligible)])
    for address in chunk:
      quality = rejected.get(address)
      if quality is None:
        quality = "available" if address in updated else "counter_rejected"
      self._quality[address] = quality
      self._message_counts[address][quality] += 1
      if quality == "available":
        self._last_accepted[address] = t_ns

  def snapshot(self, t_ns: int) -> Snapshot:
    if not isinstance(t_ns, int) or t_ns < 0 or t_ns < self._last_can_ns or t_ns < self._last_snapshot_ns:
      raise ValueError("Snapshot cannot precede ingested CAN or the previous snapshot")
    self._last_snapshot_ns = t_ns
    values = {field: None for field in FIELD_EVIDENCE}
    quality = {field: "unconfirmed" for field in FIELD_EVIDENCE}
    age_ns = {field: None for field in FIELD_EVIDENCE}
    for field, (address, signal) in SIGNALS.items():
      last = self._last_accepted.get(address)
      age = None if last is None else t_ns - last
      age_ns[field] = age
      status = self._quality[address]
      if status == "available" and age > self.parser.message_states[address].timeout_threshold:
        status = "stale"
      quality[field] = status
      if status != "available":
        continue
      value = self.parser.vl[address][signal]
      if field == "gearShifter":
        value = {1: "park", 2: "reverse", 4: "drive"}.get(int(value))
        if value is None:
          quality[field] = "unsupported_enum"
      elif field == "gasPressed":
        value = value > 3
      elif field in ("leftBlinker", "rightBlinker", "brakePressed", "cruiseState.standstill"):
        value = bool(value)
      values[field] = value
    wheels = [values[f"wheelSpeeds.{w}"] for w in ("fl", "fr", "rl", "rr")]
    quality["vEgoRaw"] = quality["wheelSpeeds.fl"]
    age_ns["vEgoRaw"] = age_ns["wheelSpeeds.fl"]
    if all(v is not None for v in wheels):
      values["vEgoRaw"] = sum(wheels) / 4
    return Snapshot(t_ns, values, quality, age_ns)

  def diagnostics(self) -> dict:
    return {"runtime": RUNTIME, "counts": dict(self._counts),
            "messages": {f"0x{a:X}": {**dict(self._message_counts[a]),
                         "last_accepted_ns": self._last_accepted.get(a),
                         "last_quality": self._quality[a],
                         "official_counter_fail": self.parser.message_states[a].counter_fail,
                         "timeout_ns": int(self.parser.message_states[a].timeout_threshold),
                         "checksum_check_enabled": any(s.calc_checksum is not None for s in self.parser.message_states[a].signals),
                         "counter_check_enabled": any(s.name == "COUNTER" for s in self.parser.message_states[a].signals),
                         "integrity_evidence": {
                           "vehicle_confirmed": False,
                           "note": ("Observed sum8 complement and low-nibble counter candidates; official parser tolerance, not vehicle-validated fault handling."
                                    if a == 0x123 else
                                    "Checksum/counter unconfirmed and not checked; only source, Event validity, DLC and freshness are gated."
                                    if a == 0x220 else
                                    "Existing observation/public-reference checks only; not independently vehicle-confirmed.")}}
                         for a in MESSAGES},
            "canValid_policy": "Always false: incomplete research projection, not an RX-fault diagnosis",
            "vehicle_semantics_independently_confirmed": False, "actuation_allowed": False}
