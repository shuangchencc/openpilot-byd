"""Failure and evidence boundaries for three newly connected RX candidates.

All payloads are synthetic fixed byte vectors. These are decoder tests, not
proof of actual pedal switches, ACC meaning, checksum ownership or safe control.
"""
from pathlib import Path
import sys
import unittest

LAB = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LAB))

from songplus_readonly.carstate import FIELD_EVIDENCE, SongPlusReadonlyCarState
from opendbc.car import structs


T0 = 1_000_000_000
GAS_LOW = bytes.fromhex("03000000000010ec")
GAS_HIGH = bytes.fromhex("04000000000010eb")
BRAKE_TRUE = bytes.fromhex("00000000000801f6")
BRAKE_FALSE = bytes.fromhex("00000000000001fe")
# No checksum/counter is claimed for 0x220. These deliberate non-sum255
# fixtures verify that an unknown integrity protocol is not invented.
STOP_TRUE = bytes.fromhex("0001000000000000")
STOP_FALSE = bytes.fromhex("0000000000000000")
SPECS = [("gasPressed", 0x342, GAS_HIGH, GAS_LOW, 200_000_000),
         ("brakePressed", 0x123, BRAKE_TRUE, BRAKE_FALSE, 100_000_000),
         ("cruiseState.standstill", 0x220, STOP_TRUE, STOP_FALSE, 200_000_000)]


def counter_payload(address, payload, counter):
  """Change synthetic integrity sequence only; no production packing API."""
  raw = bytearray(payload)
  if address == 0x123:
    raw[6] = (raw[6] & 0xF0) | counter
  elif address == 0x342:
    raw[6] = (raw[6] & 0x0F) | (counter << 4)
  else:
    raise ValueError("No counter rule specified for this test address")
  raw[7] = (255 - sum(raw[:7])) % 256
  return bytes(raw)


def initial_frames():
  return [(address, yes, 0) for _, address, yes, _, _ in SPECS]


class CandidateFieldTests(unittest.TestCase):
  def test_gas_threshold_uses_raw_byte_boundary_zero_three_four_255(self):
    vectors = [("00000000000010ef", False), ("03000000000010ec", False),
               ("04000000000010eb", True), ("ff000000000010f0", True)]
    for hex_data, expected in vectors:
      with self.subTest(raw_byte=hex_data[:2]):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, [(0x342, bytes.fromhex(hex_data), 0)])
        state = prototype.snapshot(T0)
        self.assertIs(state.values["gasPressed"], expected)
        self.assertEqual(state.quality["gasPressed"], "available")

  def test_pedal_amount_frame_cannot_initialize_brake_pressed(self):
    prototype = SongPlusReadonlyCarState()
    # Large brake amount in 0x342 still does not establish the 0x123 bit.
    prototype.ingest_batch(T0, [(0x342, bytes.fromhex("04320000000010b9"), 0)])
    state = prototype.snapshot(T0)
    self.assertAlmostEqual(state.values["brake"], 0.50)
    self.assertIs(state.values["gasPressed"], True)
    self.assertIsNone(state.values["brakePressed"])
    self.assertEqual(state.quality["brakePressed"], "unseen")

  def test_brake_bit_frame_cannot_initialize_gas_pressed_or_brake_amount(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x123, BRAKE_TRUE, 0)])
    state = prototype.snapshot(T0)
    self.assertIs(state.values["brakePressed"], True)
    self.assertIsNone(state.values["gasPressed"])
    self.assertIsNone(state.values["brake"])
    self.assertEqual(state.quality["gasPressed"], "unseen")

  def test_independent_missing_false_and_true_are_distinguishable(self):
    for field, address, yes, no, _ in SPECS:
      with self.subTest(field=field):
        prototype = SongPlusReadonlyCarState()
        self.assertIsNone(prototype.snapshot(T0).values[field])
        prototype.ingest_batch(T0, [(address, no, 0)])
        state = prototype.snapshot(T0)
        self.assertIs(state.values[field], False)
        self.assertEqual(state.quality[field], "available")
        true_frame = counter_payload(address, yes, 2) if address != 0x220 else yes
        prototype.ingest_batch(T0 + 10_000_000, [(address, true_frame, 0)])
        self.assertIs(prototype.snapshot(T0 + 10_000_000).values[field], True)

  def test_wrong_raw_sources_cannot_initialize_candidates(self):
    for field, address, yes, _, _ in SPECS:
      for source in [1, 2, 128, 130, 192, 197]:
        with self.subTest(field=field, raw_src=source):
          prototype = SongPlusReadonlyCarState()
          prototype.ingest_batch(T0, [(address, yes, source)])
          state = prototype.snapshot(T0)
          self.assertIsNone(state.values[field])
          self.assertEqual(state.quality[field], "unseen")

  def test_wrong_raw_source_does_not_replace_or_refresh_existing_candidate(self):
    for field, address, yes, no, timeout in SPECS:
      with self.subTest(field=field):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, [(address, yes, 0)])
        prototype.ingest_batch(T0 + 1, [(address, no, 128)])
        self.assertIs(prototype.snapshot(T0 + 1).values[field], True)
        prototype.ingest_batch(T0 + timeout + 1, [(address, no, 128)])
        stale = prototype.snapshot(T0 + timeout + 1)
        self.assertIsNone(stale.values[field])
        self.assertEqual(stale.quality[field], "stale")

  def test_invalid_event_hides_only_its_candidate_message(self):
    for field, address, _, no, _ in SPECS:
      with self.subTest(field=field):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, initial_frames())
        prototype.ingest_batch(T0 + 10_000_000, [(address, no, 0)], event_valid=False)
        state = prototype.snapshot(T0 + 10_000_000)
        self.assertIsNone(state.values[field])
        self.assertEqual(state.quality[field], "invalid_event")
        self.assertEqual(state.age_ns[field], 10_000_000)
        for other, _, _, _, _ in SPECS:
          if other != field:
            self.assertIs(state.values[other], True)

  def test_exact_length_failure_hides_only_its_candidate_message(self):
    for field, address, _, no, _ in SPECS:
      for bad in [b"", no[:-1], no + b"\x00", no.ljust(64, b"\x00")]:
        with self.subTest(field=field, length=len(bad)):
          prototype = SongPlusReadonlyCarState()
          prototype.ingest_batch(T0, initial_frames())
          prototype.ingest_batch(T0 + 10_000_000, [(address, bad, 0)])
          state = prototype.snapshot(T0 + 10_000_000)
          self.assertIsNone(state.values[field])
          self.assertEqual(state.quality[field], "bad_length")
          for other, _, _, _, _ in SPECS:
            if other != field:
              self.assertIs(state.values[other], True)

  def test_each_declared_timeout_boundary_keeps_age_and_then_hides_field(self):
    for field, address, yes, _, timeout in SPECS:
      with self.subTest(field=field):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, [(address, yes, 0)])
        self.assertIs(prototype.snapshot(T0 + timeout).values[field], True)
        state = prototype.snapshot(T0 + timeout + 1)
        self.assertIsNone(state.values[field])
        self.assertEqual(state.quality[field], "stale")
        self.assertEqual(state.age_ns[field], timeout + 1)

  def test_bad_checksum_masks_gas_or_brake_without_masking_the_other(self):
    for field, address, yes, _, _ in SPECS[:2]:
      with self.subTest(field=field):
        prototype = SongPlusReadonlyCarState()
        prototype.ingest_batch(T0, initial_frames())
        good = counter_payload(address, yes, 2)
        bad = good[:-1] + bytes([good[-1] ^ 1])
        prototype.ingest_batch(T0 + 10_000_000, [(address, bad, 0)])
        state = prototype.snapshot(T0 + 10_000_000)
        self.assertIsNone(state.values[field])
        self.assertEqual(state.quality[field], "bad_checksum")
        for other, _, _, _, _ in SPECS:
          if other != field:
            self.assertIs(state.values[other], True)
        prototype.ingest_batch(T0 + 20_000_000, [(address, counter_payload(address, yes, 3), 0)])
        self.assertIs(prototype.snapshot(T0 + 20_000_000).values[field], True)

  def test_brake_counter_uses_low_nibble_and_allows_wrap(self):
    prototype = SongPlusReadonlyCarState()
    for index, counter in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0]):
      prototype.ingest_batch(T0 + index * 10_000_000, [(0x123, counter_payload(0x123, BRAKE_TRUE, counter), 0)])
    state = prototype.snapshot(T0 + 150_000_000)
    self.assertIs(state.values["brakePressed"], True)
    diagnostics = prototype.diagnostics()["messages"]["0x123"]
    self.assertEqual(diagnostics.get("counter_discontinuities", 0), 0)
    self.assertEqual(diagnostics["official_counter_fail"], 0)
    self.assertIs(diagnostics["checksum_check_enabled"], True)
    self.assertIs(diagnostics["counter_check_enabled"], True)
    self.assertIs(diagnostics["integrity_evidence"]["vehicle_confirmed"], False)
    self.assertTrue(diagnostics["integrity_evidence"]["note"])

  def test_brake_single_counter_gap_uses_official_tolerance(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, [(0x123, BRAKE_TRUE, 0)])
    prototype.ingest_batch(T0 + 10_000_000, [(0x123, counter_payload(0x123, BRAKE_FALSE, 3), 0)])
    self.assertIs(prototype.snapshot(T0 + 10_000_000).values["brakePressed"], False)
    self.assertEqual(prototype.diagnostics()["messages"]["0x123"]["counter_discontinuities"], 1)

  def test_brake_counter_replay_rejects_and_recovers_on_next_sequence(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, initial_frames())
    for step in range(1, 6):
      prototype.ingest_batch(T0 + step * 10_000_000, [(0x123, BRAKE_TRUE, 0)])
    rejected = prototype.snapshot(T0 + 50_000_000)
    self.assertIsNone(rejected.values["brakePressed"])
    self.assertEqual(rejected.quality["brakePressed"], "counter_rejected")
    self.assertIs(rejected.values["gasPressed"], True)
    self.assertIs(rejected.values["cruiseState.standstill"], True)
    prototype.ingest_batch(T0 + 60_000_000, [(0x123, counter_payload(0x123, BRAKE_FALSE, 2), 0)])
    self.assertIs(prototype.snapshot(T0 + 60_000_000).values["brakePressed"], False)

  def test_standstill_does_not_invent_checksum_or_counter_checks(self):
    prototype = SongPlusReadonlyCarState()
    for step in range(8):
      prototype.ingest_batch(T0 + step * 20_000_000, [(0x220, STOP_TRUE, 0)])
    self.assertIs(prototype.snapshot(T0 + 140_000_000).values["cruiseState.standstill"], True)
    diagnostics = prototype.diagnostics()["messages"]["0x220"]
    self.assertIs(diagnostics["checksum_check_enabled"], False)
    self.assertIs(diagnostics["counter_check_enabled"], False)
    self.assertEqual(diagnostics.get("checksum_checked", 0), 0)
    self.assertEqual(diagnostics.get("counter_discontinuities", 0), 0)
    self.assertIs(diagnostics["integrity_evidence"]["vehicle_confirmed"], False)
    self.assertTrue(diagnostics["integrity_evidence"]["note"])

  def test_candidates_are_empirical_and_do_not_promote_acc_enabled_available(self):
    for field, address, _, _, _ in SPECS:
      with self.subTest(field=field):
        evidence = FIELD_EVIDENCE[field]
        self.assertEqual(evidence["tier"], "empirical_candidate")
        self.assertIs(evidence["candidate"], True)
        self.assertIs(evidence["vehicle_confirmed"], False)
        self.assertEqual(evidence["raw_src"], 0)
        self.assertEqual(evidence["address"], f"0x{address:X}")
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, initial_frames())
    state = prototype.snapshot(T0)
    for field in ["cruiseState.enabled", "cruiseState.available", "cruiseState.speed", "standstill"]:
      with self.subTest(field=field):
        self.assertEqual(FIELD_EVIDENCE[field]["tier"], "unconfirmed")
        self.assertIsNone(state.values[field])
        self.assertEqual(state.quality[field], "unconfirmed")

  def test_projection_writes_nested_cruise_standstill_without_claiming_can_valid(self):
    prototype = SongPlusReadonlyCarState()
    prototype.ingest_batch(T0, initial_frames())
    state = prototype.snapshot(T0)
    projected = state.to_opendbc()
    self.assertEqual(projected.schema.node.id, structs.CarState.schema.node.id)
    self.assertIs(projected.gasPressed, True)
    self.assertIs(projected.brakePressed, True)
    self.assertIs(projected.cruiseState.standstill, True)
    self.assertIs(projected.canValid, False)
    for field in ["enabled", "available"]:
      self.assertIs(getattr(projected.cruiseState, field), False)
      self.assertIsNone(state.values[f"cruiseState.{field}"])
      self.assertEqual(state.quality[f"cruiseState.{field}"], "unconfirmed")

  def test_missing_and_stale_projection_defaults_keep_unavailable_sidecar(self):
    prototype = SongPlusReadonlyCarState()
    initial = prototype.snapshot(T0)
    self.assertIs(initial.to_opendbc().cruiseState.standstill, False)
    self.assertIsNone(initial.values["cruiseState.standstill"])
    self.assertEqual(initial.quality["cruiseState.standstill"], "unseen")
    prototype.ingest_batch(T0, initial_frames())
    expired = prototype.snapshot(T0 + 200_000_001)
    projection = expired.to_opendbc()
    for field, _, _, _, _ in SPECS:
      self.assertIsNone(expired.values[field])
      self.assertEqual(expired.quality[field], "stale")
    self.assertIs(projection.gasPressed, False)
    self.assertIs(projection.brakePressed, False)
    self.assertIs(projection.cruiseState.standstill, False)
    self.assertIs(projection.canValid, False)


if __name__ == "__main__":
  unittest.main()
