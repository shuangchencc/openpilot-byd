"""Synthetic failure tests for the receive-only, nullable Song state prototype.

Fixed payloads below are independent hand-constructed decoding vectors. Tests
neither use a CAN packer nor import vehicle/vendor implementations. They exercise
offline framing, validity and evidence boundaries, not physical vehicle truth.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

LAB = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAB))

from songplus_readonly.carstate import FIELD_EVIDENCE, SongPlusReadonlyCarState
from opendbc.car import structs


T0 = 1_000_000_000

# Negative/positive angle with counter 1. Its checksum is deliberately not
# synthesized: no confirmed 0x11F checksum rule exists in the evidence.
ANGLE_NEGATIVE = bytes.fromhex("2efb00ff01")  # -1234 * 0.1 degrees
ANGLE_POSITIVE = bytes.fromhex("d20400ff01")  # +1234 * 0.1 degrees
WHEELS = bytes.fromhex("6400c8c012901160")  # 100, 200, 300, 400; counter 1
EPS_NEGATIVE = bytes.fromhex("00000000b70e102a")  # signed 12-bit -329
EPS_POSITIVE = bytes.fromhex("0000000079011075")  # signed 12-bit +377
PEDALS = bytes.fromhex("19320000000010a4")  # gas 25, brake 50; counter 1
BLINKERS = bytes.fromhex("0000000060000100")  # left and right; counter 1
GEAR_DRIVE = bytes.fromhex("0000000000040000")


def next_counter(payload: bytes, counter: int, *, angle: bool = False,
                 blinkers: bool = False, checksum: bool = True) -> bytes:
  """Mutate only a synthetic integrity sequence, never generate vehicle output."""
  result = bytearray(payload)
  if angle:
    result[4] = (result[4] & 0xF0) | counter
  elif blinkers:
    result[6] = (result[6] & 0xF0) | counter
  else:
    result[6] = (result[6] & 0x0F) | (counter << 4)
  if checksum:
    result[7] = (255 - sum(result[:7])) % 256
  return bytes(result)


def all_frames():
  return [(0x11F, ANGLE_NEGATIVE, 0), (0x1F0, WHEELS, 0),
          (0x1FC, EPS_NEGATIVE, 0), (0x342, PEDALS, 0),
          (0x133, BLINKERS, 0), (0x242, GEAR_DRIVE, 0)]


class ReadonlyCarStateTests(unittest.TestCase):
  def test_startup_does_not_expose_parser_zero_defaults_as_measurements(self):
    prototype = SongPlusReadonlyCarState()
    state = prototype.snapshot(T0)
    self.assertTrue(state.values)
    for name, value in state.values.items():
      with self.subTest(field=name):
        self.assertIsNone(value)

  def test_signed_angle_fixed_vectors(self):
    for payload, expected in [(ANGLE_NEGATIVE, -123.4), (ANGLE_POSITIVE, 123.4)]:
      with self.subTest(expected=expected):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, [(0x11F, payload, 0)])
        self.assertAlmostEqual(prototype.snapshot(T0).values["steeringAngleDeg"], expected)

  def test_empirical_wheel_layout_and_mean_use_one_fresh_frame(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x1F0, WHEELS, 0)])
    state = prototype.snapshot(T0)
    expected = [2.013888888888889, 4.027777777777778, 6.041666666666667, 8.055555555555556]
    for name, value in zip(["fl", "fr", "rl", "rr"], expected, strict=True):
      self.assertAlmostEqual(state.values[f"wheelSpeeds.{name}"], value)
    self.assertAlmostEqual(state.values["vEgoRaw"], 5.034722222222222)
    self.assertIsNone(state.values["vEgo"])
    self.assertIsNone(state.values["aEgo"])
    self.assertIsNone(state.values["standstill"])

  def test_eps_raw_signed_field_does_not_invent_driver_torque(self):
    for payload, expected in [(EPS_NEGATIVE, -329), (EPS_POSITIVE, 377)]:
      with self.subTest(expected=expected):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, [(0x1FC, payload, 0)])
        state = prototype.snapshot(T0)
        self.assertEqual(state.values["steeringTorqueEps"], expected)
        self.assertIsNone(state.values["steeringTorque"])
        self.assertIsNone(state.values["steeringPressed"])

  def test_pedal_values_and_gas_candidate_do_not_invent_brake_pressed(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x342, PEDALS, 0)])
    state = prototype.snapshot(T0)
    self.assertAlmostEqual(state.values["gas"], 0.25)
    self.assertAlmostEqual(state.values["brake"], 0.50)
    self.assertIs(state.values["gasPressed"], True)
    self.assertIsNone(state.values["brakePressed"])
    self.assertEqual(state.quality["brakePressed"], "unseen")

  def test_received_blinker_false_is_distinct_from_unavailable(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x133, BLINKERS, 0)])
    active = prototype.snapshot(T0)
    self.assertIs(active.values["leftBlinker"], True)
    self.assertIs(active.values["rightBlinker"], True)
    cleared = next_counter(bytes.fromhex("0000000000000100"), 2,
                           blinkers=True, checksum=False)
    prototype.ingest_batch(T0 + 50_000_000, [(0x133, cleared, 0)])
    inactive = prototype.snapshot(T0 + 50_000_000)
    self.assertIs(inactive.values["leftBlinker"], False)
    self.assertIs(inactive.values["rightBlinker"], False)

  def test_only_observed_gear_labels_are_exposed(self):
    expected = {1: "park", 2: "reverse", 4: "drive"}
    for gear in range(8):
      with self.subTest(gear=gear):
        prototype = SongPlusReadonlyCarState()
        payload = bytearray(GEAR_DRIVE)
        payload[5] = gear
        prototype.ingest_batch(T0, [(0x242, bytes(payload), 0)])
        state = prototype.snapshot(T0)
        self.assertEqual(state.values["gearShifter"], expected.get(gear))
        self.assertEqual(state.quality["gearShifter"], "available" if gear in expected else "unsupported_enum")

  def test_raw_source_flags_are_never_masked_into_receive_bus_zero(self):
    for source in [1, 2, 128, 130, 192, 197]:
      with self.subTest(source=source):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, source)])
        self.assertIsNone(prototype.snapshot(T0).values["steeringAngleDeg"])

  def test_wrong_source_cannot_replace_a_fresh_observation(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, 0)])
    prototype.ingest_batch(T0 + 1_000_000, [(0x11F, ANGLE_POSITIVE, 128)])
    self.assertAlmostEqual(prototype.snapshot(T0 + 1_000_000).values["steeringAngleDeg"], -123.4)

  def test_invalid_can_event_cannot_initialize_any_field(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, all_frames(), event_valid=False)
    self.assertTrue(all(v is None for v in prototype.snapshot(T0).values.values()))

  def test_invalid_event_after_good_frame_hides_cached_measurement(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, 0)])
    prototype.ingest_batch(T0 + 10_000_000, [(0x11F, ANGLE_POSITIVE, 0)], event_valid=False)
    state = prototype.snapshot(T0 + 10_000_000)
    self.assertIsNone(state.values["steeringAngleDeg"])
    self.assertEqual(state.quality["steeringAngleDeg"], "invalid_event")
    self.assertEqual(state.age_ns["steeringAngleDeg"], 10_000_000)
    prototype.ingest_batch(T0 + 20_000_000,
                           [(0x11F, next_counter(ANGLE_POSITIVE, 2, angle=True, checksum=False), 0)])
    self.assertAlmostEqual(prototype.snapshot(T0 + 20_000_000).values["steeringAngleDeg"], 123.4)

  def test_exact_dlc_is_enforced_before_official_parser(self):
    for address, payload, _, field in [
      (0x11F, ANGLE_NEGATIVE, 0, "steeringAngleDeg"),
      (0x1F0, WHEELS, 0, "vEgoRaw"),
      (0x1FC, EPS_NEGATIVE, 0, "steeringTorqueEps"),
      (0x342, PEDALS, 0, "brake"),
      (0x133, BLINKERS, 0, "leftBlinker"),
      (0x242, GEAR_DRIVE, 0, "gearShifter"),
    ]:
      for bad in [b"", payload[:-1], payload + b"\x00", payload.ljust(64, b"\x00")]:
        with self.subTest(address=hex(address), length=len(bad)):
          prototype = SongPlusReadonlyCarState()
          prototype.ingest_batch(T0, [(address, bad, 0)])
          self.assertIsNone(prototype.snapshot(T0).values[field])

  def test_bad_checksum_never_initializes_checksummed_signals(self):
    for address, payload, field in [(0x1F0, WHEELS, "vEgoRaw"),
                                    (0x1FC, EPS_NEGATIVE, "steeringTorqueEps"),
                                    (0x342, PEDALS, "brake")]:
      with self.subTest(address=hex(address)):
        prototype = SongPlusReadonlyCarState()
        corrupt = payload[:-1] + bytes([payload[-1] ^ 1])
        prototype.ingest_batch(T0, [(address, corrupt, 0)])
        self.assertIsNone(prototype.snapshot(T0).values[field])

  def test_bad_checksum_hides_cached_field_until_next_good_frame(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x1FC, EPS_NEGATIVE, 0)])
    self.assertEqual(prototype.snapshot(T0).values["steeringTorqueEps"], -329)
    second = next_counter(EPS_POSITIVE, 2)
    bad = second[:-1] + bytes([second[-1] ^ 1])
    prototype.ingest_batch(T0 + 20_000_000, [(0x1FC, bad, 0)])
    state = prototype.snapshot(T0 + 20_000_000)
    self.assertIsNone(state.values["steeringTorqueEps"])
    self.assertEqual(state.quality["steeringTorqueEps"], "bad_checksum")
    self.assertEqual(state.age_ns["steeringTorqueEps"], 20_000_000)
    prototype.ingest_batch(T0 + 40_000_000, [(0x1FC, next_counter(EPS_POSITIVE, 3), 0)])
    self.assertEqual(prototype.snapshot(T0 + 40_000_000).values["steeringTorqueEps"], 377)

  def test_freshness_bounds_use_each_messages_declared_frequency(self):
    for address, payload, field, timeout in [
      (0x11F, ANGLE_NEGATIVE, "steeringAngleDeg", 100_000_000),
      (0x1F0, WHEELS, "vEgoRaw", 200_000_000),
      (0x133, BLINKERS, "leftBlinker", 500_000_000),
    ]:
      with self.subTest(address=hex(address)):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, [(address, payload, 0)])
        self.assertIsNotNone(prototype.snapshot(T0 + timeout).values[field])
        stale = prototype.snapshot(T0 + timeout + 1)
        self.assertIsNone(stale.values[field])
        self.assertEqual(stale.quality[field], "stale")
        self.assertEqual(stale.age_ns[field], timeout + 1)

  def test_irrelevant_frames_do_not_keep_measurements_fresh(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, 0)])
    prototype.ingest_batch(T0 + 100_000_001, [(0x7FF, bytes(8), 0)])
    self.assertIsNone(prototype.snapshot(T0 + 100_000_001).values["steeringAngleDeg"])

  def test_single_counter_gap_keeps_official_tolerance_and_wrap_recovers(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x1FC, EPS_NEGATIVE, 0)])
    prototype.ingest_batch(T0 + 20_000_000, [(0x1FC, next_counter(EPS_POSITIVE, 3), 0)])
    self.assertEqual(prototype.snapshot(T0 + 20_000_000).values["steeringTorqueEps"], 377)
    self.assertEqual(prototype.diagnostics()["messages"]["0x1FC"]["counter_discontinuities"], 1)
    for step, counter in enumerate(range(4, 17), start=2):
      prototype.ingest_batch(T0 + step * 20_000_000,
                             [(0x1FC, next_counter(EPS_NEGATIVE, counter % 16), 0)])
    self.assertEqual(prototype.snapshot(T0 + 14 * 20_000_000).values["steeringTorqueEps"], -329)

  def test_repeated_counter_replay_is_not_accepted_forever(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x1FC, EPS_NEGATIVE, 0)])
    for step in range(1, 6):
      prototype.ingest_batch(T0 + step * 20_000_000, [(0x1FC, EPS_NEGATIVE, 0)])
    rejected = prototype.snapshot(T0 + 100_000_000)
    self.assertIsNone(rejected.values["steeringTorqueEps"])
    self.assertEqual(rejected.quality["steeringTorqueEps"], "counter_rejected")
    prototype.ingest_batch(T0 + 120_000_000, [(0x1FC, next_counter(EPS_POSITIVE, 2), 0)])
    self.assertEqual(prototype.snapshot(T0 + 120_000_000).values["steeringTorqueEps"], 377)

  def test_ingest_time_regression_is_rejected_instead_of_sorted(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, 0)])
    with self.assertRaises(ValueError):
      prototype.ingest_batch(T0 - 1, [(0x11F, ANGLE_POSITIVE, 0)])

  def test_snapshot_must_not_read_future_can_or_move_backwards(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, 0)])
    with self.assertRaises(ValueError):
      prototype.snapshot(T0 - 1)
    prototype.snapshot(T0 + 1)
    with self.assertRaises(ValueError):
      prototype.snapshot(T0)

  def test_same_timestamp_preserves_frame_order(self):
    prototype = SongPlusReadonlyCarState()
    positive_counter_two = next_counter(ANGLE_POSITIVE, 2, angle=True, checksum=False)
    prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, 0), (0x11F, positive_counter_two, 0)])
    self.assertAlmostEqual(prototype.snapshot(T0).values["steeringAngleDeg"], 123.4)

  def test_unconfirmed_safety_booleans_remain_nullable_with_all_frames_present(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, all_frames())
    state = prototype.snapshot(T0)
    for field in ["steeringPressed", "standstill",
                  "doorOpen", "seatbeltUnlatched", "steerFaultTemporary",
                  "steerFaultPermanent", "cruiseState.enabled", "cruiseState.available"]:
      with self.subTest(field=field):
        self.assertIsNone(state.values[field])
        self.assertEqual(state.quality[field], "unconfirmed")

  def test_evidence_tiers_never_claim_vehicle_confirmed_semantics(self):
    supported = ["steeringAngleDeg", "steeringTorqueEps", "brake", "leftBlinker", "rightBlinker"]
    empirical = ["gas", "gearShifter", "vEgoRaw", "wheelSpeeds.fl", "wheelSpeeds.fr",
                 "wheelSpeeds.rl", "wheelSpeeds.rr", "gasPressed", "brakePressed", "cruiseState.standstill"]
    for field, evidence in FIELD_EVIDENCE.items():
      with self.subTest(field=field):
        tier = "supported_encoding" if field in supported else "empirical_candidate" if field in empirical else "unconfirmed"
        self.assertEqual(evidence["tier"], tier)
        self.assertIs(evidence["vehicle_confirmed"], False)
        self.assertTrue(evidence["note"])

  def test_unresolved_checksums_are_reported_without_fabricated_validation(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, all_frames())
    diagnostics = prototype.diagnostics()
    for address in ["0x11F", "0x133", "0x242"]:
      with self.subTest(address=address):
        message = diagnostics["messages"][address]
        self.assertIs(message["checksum_check_enabled"], False)
        self.assertEqual(message.get("checksum_checked", 0), 0)
    for address in ["0x1F0", "0x1FC", "0x342"]:
      with self.subTest(address=address):
        self.assertEqual(diagnostics["messages"][address]["checksum_checked"], 1)
    self.assertIs(diagnostics["vehicle_semantics_independently_confirmed"], False)
    self.assertIs(diagnostics["actuation_allowed"], False)

  def test_official_schema_projection_is_typed_lossy_and_always_non_valid(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, all_frames())
    state = prototype.snapshot(T0)
    projected = state.to_opendbc()
    self.assertEqual(projected.schema.node.id, structs.CarState.schema.node.id)
    self.assertIs(projected.canValid, False)
    self.assertAlmostEqual(projected.steeringAngleDeg, -123.4, places=4)
    self.assertEqual(projected.gearShifter, "drive")
    for field in ["doorOpen", "seatbeltUnlatched", "steeringPressed"]:
      with self.subTest(field=field):
        self.assertIs(getattr(projected, field), False)
        self.assertIsNone(state.values[field])
        self.assertEqual(state.quality[field], "unconfirmed")
    self.assertIs(projected.gasPressed, True)
    self.assertIs(state.values["gasPressed"], True)
    self.assertIs(projected.brakePressed, False)
    self.assertIsNone(state.values["brakePressed"])
    self.assertEqual(state.quality["brakePressed"], "unseen")
    self.assertEqual(projected.deprecated.gas, 0)
    self.assertEqual(projected.deprecated.brake, 0)
    self.assertAlmostEqual(state.values["gas"], 0.25)
    self.assertAlmostEqual(state.values["brake"], 0.50)

  def test_one_messages_refresh_does_not_refresh_another(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x11F, ANGLE_NEGATIVE, 0), (0x1FC, EPS_NEGATIVE, 0)])
    prototype.ingest_batch(T0 + 200_000_001,
                           [(0x11F, next_counter(ANGLE_POSITIVE, 2, angle=True, checksum=False), 0)])
    state = prototype.snapshot(T0 + 200_000_001)
    self.assertAlmostEqual(state.values["steeringAngleDeg"], 123.4)
    self.assertIsNone(state.values["steeringTorqueEps"])
    self.assertEqual(state.age_ns["steeringAngleDeg"], 0)
    self.assertEqual(state.age_ns["steeringTorqueEps"], 200_000_001)

  def test_same_batch_good_frame_after_checksum_failure_recovers_in_order(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x1FC, EPS_NEGATIVE, 0)])
    second = next_counter(EPS_POSITIVE, 2)
    bad = second[:-1] + bytes([second[-1] ^ 1])
    prototype.ingest_batch(T0 + 20_000_000,
                           [(0x1FC, bad, 0), (0x1FC, next_counter(EPS_POSITIVE, 3), 0)])
    state = prototype.snapshot(T0 + 20_000_000)
    self.assertEqual(state.values["steeringTorqueEps"], 377)
    self.assertEqual(state.quality["steeringTorqueEps"], "available")
    self.assertEqual(prototype.diagnostics()["messages"]["0x1FC"]["bad_checksum"], 1)


if __name__ == "__main__":
  unittest.main()
