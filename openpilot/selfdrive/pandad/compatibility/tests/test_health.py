"""Offline P1 health acceptance. A real host header probe is required, never skipped."""
import json
import math
import os
from pathlib import Path
import struct
import unittest

from compatibility import health as h

# Literal vectors and offsets are independent test oracles, not encoder output.
CURRENT_HEX = ("04030201 3930 a602 14131211 24232221 34333231 44434241 01800000 "
               "1101 02 13 5251 01 0800 80 4b 6261 e40c b004 7271 41")
LEGACY_HEX = ("04030201 39300000 a6020000 14131211 24232221 34333231 44434241 01800000 "
              "01 00 01 02 20 2221 01 00 01 0000 0000003f 4b 00 6261 03 e40c b004 01")
CAN_HEX = ("01 04030201 01 00 03 07 02 06 7f ff 14131211 24232221 34333231 44434241 "
           "54535251 64636261 74737271 f401 d007 01 01 00 84838281 94939291 a4a3a2a1 b4b3b2b1")
CURRENT_RAW = {
  "uptime": 0x01020304, "voltage": 12345, "current": 678, "safety_tx_blocked": 0x11121314,
  "safety_rx_invalid": 0x21222324, "tx_buffer_overflow": 0x31323334, "rx_buffer_overflow": 0x41424344,
  "faults": 0x8001, "flags": 0x111, "car_harness_status": 2, "safety_mode": 19, "safety_param": 0x5152,
  "fault_status": 1, "alternative_experience": 8, "interrupt_load": 128, "fan_power": 75,
  "spi_error_count": 0x6162, "sbu1_voltage_mV": 3300, "sbu2_voltage_mV": 1200,
  "sound_output_level": 0x7172, "temperature": 65,
}
CURRENT_OFFSETS = (0, 4, 6, 8, 12, 16, 20, 24, 28, 30, 31, 32, 34, 35, 37, 38, 39, 41, 43, 45, 47)
CAN_OFFSETS = (0, 1, 5, 6, 7, 8, 9, 10, 11, 12, 13, 17, 21, 25, 29, 33, 37, 41, 43, 45, 46, 47, 48, 52, 56, 60)
LEGACY_OFFSETS = (0, 4, 8, 12, 16, 20, 24, 28, 32, 33, 34, 35, 36, 37, 39, 40, 41, 42, 44, 48, 49, 50, 52, 53, 55, 57)


def current(**changes):
  return h.WireHealth(h.CURRENT_VERSION, dict(CURRENT_RAW, **changes))


def can_packet(**changes):
  return h.WireCanHealth(h.CURRENT_VERSION, dict(h.decode_can_health(bytes.fromhex(CAN_HEX), h.CURRENT_VERSION).values, **changes))


class TestVersions(unittest.TestCase):
  def test_current_exact_pair(self):
    version = h.decode_versions(bytes.fromhex("f512bcff76f2ab75"))
    self.assertEqual((version.health, version.can, version.can_health), (0xFFBC12F5, 0x75ABF276, None))
    self.assertTrue(version.control_protocol_supported)
    self.assertEqual(h.encode_versions(version), bytes.fromhex("f512bcff76f2ab75"))

  def test_all_short_overlong_and_wrong_hashes_rejected(self):
    vector = bytes.fromhex("f512bcff76f2ab75")
    for bad in [vector[:size] for size in range(8)] + [vector + b"\0", b"\0" * 8, bytes(reversed(vector))]:
      with self.subTest(bad=bad.hex()), self.assertRaises(h.HealthCodecError):
        h.decode_versions(bad)
    for index in range(8):
      bad = bytearray(vector)
      bad[index] ^= 1
      with self.subTest(byte=index), self.assertRaises(h.HealthCodecError):
        h.decode_versions(bytes(bad))

  def test_legacy_must_be_explicit_and_offline(self):
    with self.assertRaises(h.HealthCodecError):
      h.decode_versions(bytes.fromhex("100405"))
    version = h.decode_versions(bytes.fromhex("100405"), profile=h.ProtocolProfile.SP098_OFFLINE)
    self.assertFalse(version.control_protocol_supported)
    self.assertEqual(h.encode_versions(version), bytes.fromhex("100405"))
    for bad in (bytes.fromhex("110405"), bytes.fromhex("100505"), bytes.fromhex("100406"), b"\x10\x04", b"\x10\x04\x05\0",
                h.CURRENT_VERSION_BYTES):
      with self.subTest(bad=bad.hex()), self.assertRaises(h.HealthCodecError):
        h.decode_versions(bad, profile=h.ProtocolProfile.SP098_OFFLINE)

  def test_control_protocol_property_rejects_coerced_or_wrong_types(self):
    versions = (
      h.ProtocolVersion(h.ProtocolProfile.CURRENT, float(0xFFBC12F5), 0x75ABF276, None),
      h.ProtocolVersion(h.ProtocolProfile.CURRENT, 0xFFBC12F5, float(0x75ABF276), None),
      h.ProtocolVersion(h.ProtocolProfile.CURRENT, True, 0x75ABF276, None),
      h.ProtocolVersion(h.ProtocolProfile.CURRENT, 0xFFBC12F5, True, None),
      h.ProtocolVersion(h.ProtocolProfile.CURRENT, 0xFFBC12F5, 0x75ABF276, False),
      h.ProtocolVersion("panda-a5919516", 0xFFBC12F5, 0x75ABF276, None),
    )
    for version in versions:
      with self.subTest(version=version):
        self.assertFalse(version.control_protocol_supported)
        with self.assertRaises(h.HealthCodecError):
          h.encode_versions(version)

  def test_types_and_forged_versions_rejected(self):
    for bad in (list(h.CURRENT_VERSION_BYTES), bytearray(h.CURRENT_VERSION_BYTES), h.CURRENT_VERSION_BYTES.hex(), None):
      with self.subTest(type=type(bad)), self.assertRaises(h.HealthCodecError):
        h.decode_versions(bad)
    with self.assertRaises(h.HealthCodecError):
      h.decode_versions(h.CURRENT_VERSION_BYTES, profile="panda-a5919516")
    for version in (None, h.ProtocolVersion(h.ProtocolProfile.CURRENT, 16, 4, 5),
                    h.ProtocolVersion(h.ProtocolProfile.SP098_OFFLINE, 17, 4, 5),
                    h.ProtocolVersion("sp098-health16-can4-canhealth5-offline", 16, 4, 5)):
      with self.subTest(version=version), self.assertRaises(h.HealthCodecError):
        h.encode_versions(version)


class TestHealth(unittest.TestCase):
  def test_current_golden_encode_and_decode(self):
    wire = bytes.fromhex(CURRENT_HEX)
    self.assertEqual(len(wire), 48)
    self.assertEqual(dict(h.decode_health(wire, h.CURRENT_VERSION).values), CURRENT_RAW)
    self.assertEqual(h.encode_health(current()), wire)
    for name, offset, code in zip(h.CURRENT_HEALTH_FIELDS, CURRENT_OFFSETS, h.CURRENT_HEALTH_FORMAT[1:], strict=True):
      self.assertEqual(struct.unpack_from("<" + code, wire, offset)[0], CURRENT_RAW[name])

  def test_exact_length_for_both_layouts(self):
    for wire, version in ((bytes.fromhex(CURRENT_HEX), h.CURRENT_VERSION), (bytes.fromhex(LEGACY_HEX), h.LEGACY_VERSION)):
      for bad in [wire[:length] for length in range(len(wire))] + [wire + b"\0", wire + b"\0" * 64]:
        with self.subTest(profile=version.profile, length=len(bad)), self.assertRaises(h.HealthCodecError):
          h.decode_health(bad, version)
    for wire, wrong in ((bytes.fromhex(CURRENT_HEX), h.LEGACY_VERSION), (bytes.fromhex(LEGACY_HEX), h.CURRENT_VERSION)):
      with self.assertRaises(h.HealthCodecError):
        h.decode_health(wire, wrong)

  def test_legacy_golden_no_safety_enum_remapping(self):
    raw = h.decode_health(bytes.fromhex(LEGACY_HEX), h.LEGACY_VERSION)
    self.assertEqual(len(bytes.fromhex(LEGACY_HEX)), 58)
    self.assertEqual(raw.values["safety_mode"], 32)
    self.assertEqual(raw.values["safety_param"], 0x2122)
    self.assertEqual(raw.values["interrupt_load"], 0.5)
    self.assertEqual(raw.values["fan_stall_count"], 3)
    self.assertEqual(h.encode_health(raw), bytes.fromhex(LEGACY_HEX))
    for name, offset, code in zip(h.LEGACY_HEALTH_FIELDS, LEGACY_OFFSETS, h.LEGACY_HEALTH_FORMAT[1:], strict=True):
      self.assertEqual(struct.unpack_from("<" + code, bytes.fromhex(LEGACY_HEX), offset)[0], raw.values[name])
    view = h.normalize_health(raw, h.HealthCapabilities(True, True, True, True, True, True))
    self.assertFalse(view.control_protocol_supported)
    self.assertEqual(view.values["safety_mode"], 32)
    self.assertIsNone(view.values["temperature_c"])
    self.assertIsNone(view.values["sound_output_level"])
    self.assertIsNone(view.values["nmi_reset"])
    self.assertIsNone(view.values["hardfault_reset"])
    self.assertIsNone(view.values["spi_error_count"])
    self.assertEqual(view.values["spi_checksum_error_count"], 0x6162)

  def test_all_known_flags_and_unknown_flag_bits(self):
    for bit, name in enumerate(h.HEALTH_FLAG_NAMES):
      view = h.normalize_health(current(flags=1 << bit), h.HealthCapabilities(harness=True))
      self.assertTrue(view.values[name])
      for other in h.HEALTH_FLAG_NAMES:
        if other != name:
          self.assertFalse(view.values[other])
    for bit in range(9, 16):
      with self.subTest(bit=bit), self.assertRaises(h.HealthCodecError):
        h.encode_health(current(flags=1 << bit))

  def test_current_unknown_fault_bits_rejected(self):
    for bit in range(32):
      packet = current(faults=1 << bit)
      if bit in h.KNOWN_FAULT_BITS:
        self.assertEqual(h.decode_health(h.encode_health(packet), h.CURRENT_VERSION).values["faults"], 1 << bit)
      else:
        with self.subTest(bit=bit), self.assertRaises(h.HealthCodecError):
          h.encode_health(packet)

  def test_unknown_enums_and_current_legacy_safety_id(self):
    for field, value in (("safety_mode", 32), ("safety_mode", 255), ("car_harness_status", 3), ("fault_status", 3),
                         ("alternative_experience", 1), ("alternative_experience", 4), ("fan_power", 101)):
      with self.subTest(field=field, value=value), self.assertRaises(h.HealthCodecError):
        h.encode_health(current(**{field: value}))
    for mode in h.CURRENT_SAFETY_MODES:
      self.assertEqual(h.decode_health(h.encode_health(current(safety_mode=mode)), h.CURRENT_VERSION).values["safety_mode"], mode)
    with self.assertRaises(h.HealthCodecError):
      legacy = dict(h.decode_health(bytes.fromhex(LEGACY_HEX), h.LEGACY_VERSION).values, safety_mode=35)
      h.encode_health(h.WireHealth(h.LEGACY_VERSION, legacy))

  def test_decode_validates_semantics_not_only_encode(self):
    for offset, bad in ((28, 0), (30, 3), (31, 32), (34, 3), (38, 101)):
      wire = bytearray.fromhex(CURRENT_HEX)
      if offset == 28:
        wire[29] |= 0x80
      else:
        wire[offset] = bad
      with self.subTest(offset=offset), self.assertRaises(h.HealthCodecError):
        h.decode_health(bytes(wire), h.CURRENT_VERSION)
    wire = bytearray.fromhex(CURRENT_HEX)
    wire[24] |= 0x40
    with self.assertRaises(h.HealthCodecError):
      h.decode_health(bytes(wire), h.CURRENT_VERSION)

  def test_strict_widths_types_and_exact_fields(self):
    for field, code in zip(h.CURRENT_HEALTH_FIELDS, h.CURRENT_HEALTH_FORMAT[1:], strict=True):
      max_value = (1 << {"B": 8, "H": 16, "I": 32}[code])
      for bad in (-1, max_value, True, 1.5, "1", None):
        with self.subTest(field=field, bad=bad), self.assertRaises(h.HealthCodecError):
          h.encode_health(current(**{field: bad}))
    for values in ({}, dict(CURRENT_RAW, unexpected=0), {k: v for k, v in CURRENT_RAW.items() if k != "uptime"}):
      with self.assertRaises(h.HealthCodecError):
        h.encode_health(h.WireHealth(h.CURRENT_VERSION, values))

  def test_legacy_boolean_and_nonfinite_load_rejected(self):
    raw = dict(h.decode_health(bytes.fromhex(LEGACY_HEX), h.LEGACY_VERSION).values)
    for name in h.HEALTH_FLAG_NAMES[:7]:
      with self.subTest(name=name), self.assertRaises(h.HealthCodecError):
        h.encode_health(h.WireHealth(h.LEGACY_VERSION, dict(raw, **{name: 2})))
    for load in (math.nan, math.inf, -math.inf, -0.01, 1.01, True, "0.5"):
      with self.subTest(load=load), self.assertRaises(h.HealthCodecError):
        h.encode_health(h.WireHealth(h.LEGACY_VERSION, dict(raw, interrupt_load=load)))
    for load in (math.nan, math.inf, -math.inf, -0.01, 1.01):
      wire = bytearray.fromhex(LEGACY_HEX)
      wire[44:48] = struct.pack("<f", load)
      with self.subTest(wire_load=load), self.assertRaises(h.HealthCodecError):
        h.decode_health(bytes(wire), h.LEGACY_VERSION)

  def test_normalized_units_and_distinct_counter_semantics(self):
    view = h.normalize_health(current(), h.HealthCapabilities(True, True, True, True, True, True))
    self.assertAlmostEqual(view.values["voltage_v"], 12.345)
    self.assertAlmostEqual(view.values["current_a"], 0.678)
    self.assertAlmostEqual(view.values["sbu1_voltage_v"], 3.3)
    self.assertAlmostEqual(view.values["sbu2_voltage_v"], 1.2)
    self.assertEqual(view.values["temperature_c"], 25)
    self.assertEqual(view.values["interrupt_load"], 128 / 255)
    self.assertEqual(view.values["fan_power_percent"], 75)
    self.assertEqual(view.values["spi_error_count"], 0x6162)
    self.assertIsNone(view.values["spi_checksum_error_count"])
    self.assertIsNone(view.values["fan_stall_count"])
    self.assertNotIn("voltage", view.values)
    self.assertEqual(view.raw.values["voltage"], 12345)

  def test_absent_and_unknown_capabilities_never_fabricate_sensor_readings(self):
    for declared in (None, False):
      cap = h.HealthCapabilities(declared, declared, declared, declared, declared, declared)
      view = h.normalize_health(current(temperature=0, current=0, voltage=0), cap)
      for field in ("temperature_c", "sound_output_level", "fan_power_percent", "voltage_v", "current_a", "car_harness_status",
                    "ignition_line", "sbu1_voltage_v", "sbu2_voltage_v"):
        with self.subTest(declared=declared, field=field):
          self.assertIsNone(view.values[field])
    actual_sensor = h.normalize_health(current(temperature=0), h.HealthCapabilities(temperature=True))
    self.assertEqual(actual_sensor.values["temperature_c"], -40)
    for value in (0, 1, "yes", [], object()):
      with self.subTest(value=value), self.assertRaises(h.HealthCodecError):
        h.HealthCapabilities(temperature=value)

  def test_snapshots_are_immutable(self):
    values = dict(CURRENT_RAW)
    packet = h.WireHealth(h.CURRENT_VERSION, values)
    values["uptime"] = 0
    self.assertEqual(packet.values["uptime"], 0x01020304)
    with self.assertRaises(TypeError):
      packet.values["uptime"] = 0
    view = h.normalize_health(packet, h.HealthCapabilities())
    with self.assertRaises(TypeError):
      view.values["uptime"] = 0

  def test_saturation_and_rounding_are_explicit_not_raw_wrap(self):
    for voltage, expected in ((0, 0), (65534, 65534), (65535, 65535), (65536, 65535), (0xFFFFFFFF, 65535)):
      result = h.quantize_current_measurements(voltage_mv=voltage, current_ma=voltage, interrupt_load=0.5, temperature_c=25.5)
      self.assertEqual(dict(result), {"voltage": expected, "current": expected, "interrupt_load": 128, "temperature": 66})
    self.assertEqual(h.quantize_current_measurements(voltage_mv=0, current_ma=0, interrupt_load=-1, temperature_c=-100)["temperature"], 0)
    maximum = h.quantize_current_measurements(voltage_mv=0, current_ma=0, interrupt_load=2, temperature_c=300)
    self.assertEqual((maximum["interrupt_load"], maximum["temperature"]), (255, 255))
    self.assertEqual(h.normalize_health(current(temperature=255), h.HealthCapabilities(temperature=True)).values["temperature_c"], 215)

  def test_quantization_rejects_nonfinite_types_and_unsafe_widths(self):
    base = {"voltage_mv": 0, "current_ma": 0, "interrupt_load": 0.5, "temperature_c": 25.5}
    for field in base:
      bad_values = (True, "1", None, math.nan, math.inf, -math.inf, 10**1000)
      if field.endswith(("_mv", "_ma")):
        bad_values += (-1, 1 << 32, 1.5)
      else:
        bad_values += (1e300,)
      for bad in bad_values:
        with self.subTest(field=field, type=type(bad)), self.assertRaises(h.HealthCodecError):
          h.quantize_current_measurements(**dict(base, **{field: bad}))


class TestCanHealth(unittest.TestCase):
  def test_golden_encode_decode_and_kbit_units(self):
    packet = h.decode_can_health(bytes.fromhex(CAN_HEX), h.CURRENT_VERSION)
    self.assertEqual(h.encode_can_health(packet), bytes.fromhex(CAN_HEX))
    self.assertEqual(len(bytes.fromhex(CAN_HEX)), 64)
    self.assertEqual(packet.values["bus_off_cnt"], 0x01020304)
    self.assertEqual(packet.values["total_tx_checksum_error_cnt"], 0x71727374)
    self.assertEqual(packet.values["can_core_reset_cnt"], 0xB1B2B3B4)
    for name, offset, code in zip(h.CAN_HEALTH_FIELDS, CAN_OFFSETS, h.CAN_HEALTH_FORMAT[1:], strict=True):
      self.assertEqual(struct.unpack_from("<" + code, bytes.fromhex(CAN_HEX), offset)[0], packet.values[name])
    values = h.normalize_can_health(packet, can_fd=True)
    self.assertEqual(values["can_speed_bps"], 500000)
    self.assertEqual(values["can_data_speed_bps"], 2000000)
    self.assertTrue(values["bus_off"])
    self.assertEqual(values["last_data_error"], 2)

  def test_exact_size_and_version_guard(self):
    wire = bytes.fromhex(CAN_HEX)
    for bad in [wire[:size] for size in range(64)] + [wire + b"\0", wire + wire]:
      with self.subTest(size=len(bad)), self.assertRaises(h.HealthCodecError):
        h.decode_can_health(bad, h.CURRENT_VERSION)
    with self.assertRaises(h.HealthCodecError):
      h.decode_can_health(wire, h.ProtocolVersion(h.ProtocolProfile.SP098_OFFLINE, 17, 4, 5))
    legacy = h.decode_can_health(wire, h.LEGACY_VERSION)
    self.assertFalse(legacy.version.control_protocol_supported)

  def test_invalid_booleans_errors_and_counters(self):
    for field in ("bus_off", "error_warning", "error_passive", "canfd_enabled", "brs_enabled", "canfd_non_iso"):
      with self.subTest(field=field), self.assertRaises(h.HealthCodecError):
        h.encode_can_health(can_packet(**{field: 2}))
    for field in ("last_error", "last_stored_error", "last_data_error", "last_data_stored_error"):
      with self.subTest(field=field), self.assertRaises(h.HealthCodecError):
        h.encode_can_health(can_packet(**{field: 8}))
    with self.assertRaises(h.HealthCodecError):
      h.encode_can_health(can_packet(receive_error_cnt=128))
    wire = bytearray.fromhex(CAN_HEX)
    wire[7] = 8
    with self.assertRaises(h.HealthCodecError):
      h.decode_can_health(bytes(wire), h.CURRENT_VERSION)

  def test_upstream_transient_brs_without_fd_preserved_and_marked(self):
    packet = can_packet(canfd_enabled=0, brs_enabled=1)
    wire = bytearray.fromhex(CAN_HEX)
    wire[45] = 0
    self.assertEqual(h.encode_can_health(packet), bytes(wire))
    decoded = h.decode_can_health(bytes(wire), h.CURRENT_VERSION)
    self.assertEqual((decoded.values["canfd_enabled"], decoded.values["brs_enabled"]), (0, 1))
    normalized = h.normalize_can_health(decoded, can_fd=True)
    self.assertFalse(normalized["canfd_enabled"])
    self.assertTrue(normalized["brs_enabled"])
    self.assertFalse(normalized["fd_configuration_consistent"])
    # Unknown capability still retains the explicit diagnostic mismatch.
    self.assertFalse(h.normalize_can_health(decoded, can_fd=None)["fd_configuration_consistent"])
    self.assertTrue(h.normalize_can_health(can_packet(), can_fd=True)["fd_configuration_consistent"])
    with self.assertRaises(h.HealthCodecError):
      h.normalize_can_health(decoded, can_fd=False)

  def test_all_widths_and_types(self):
    for field, code in zip(h.CAN_HEALTH_FIELDS, h.CAN_HEALTH_FORMAT[1:], strict=True):
      for bad in (-1, 1 << {"B": 8, "H": 16, "I": 32}[code], True, 1.5, "0"):
        with self.subTest(field=field, bad=bad), self.assertRaises(h.HealthCodecError):
          h.encode_can_health(can_packet(**{field: bad}))

  def test_classic_unknown_and_inconsistent_fd_capabilities(self):
    packet = can_packet(canfd_enabled=0, brs_enabled=0, canfd_non_iso=0)
    for can_fd in (False, None):
      values = h.normalize_can_health(packet, can_fd=can_fd)
      for field in ("can_data_speed_bps", "last_data_error", "last_data_stored_error", "canfd_enabled", "brs_enabled", "canfd_non_iso"):
        self.assertIsNone(values[field])
    with self.assertRaises(h.HealthCodecError):
      h.normalize_can_health(can_packet(), can_fd=False)
    with self.assertRaises(h.HealthCodecError):
      h.normalize_can_health(packet, can_fd=1)


class TestHeaderABI(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    path = os.environ.get("TEST_HEALTH_LAYOUT_JSON")
    if not path:
      raise AssertionError("TEST_HEALTH_LAYOUT_JSON is required: compile and run the host probe against the locked headers first")
    cls.probe = json.loads(Path(path).read_text())

  def test_compiled_sizes_and_every_nonbitfield_offset(self):
    self.assertIs(self.probe["host_abi_only"], True)
    self.assertEqual(self.probe["sizes"], {"health": 48, "can_health": 64, "can_packet": 72, "can_alignment": 4})
    self.assertEqual(self.probe["offsets"]["health"], dict(zip(h.CURRENT_HEALTH_FIELDS, CURRENT_OFFSETS, strict=True)))
    self.assertEqual(self.probe["offsets"]["can_health"], dict(zip(h.CAN_HEALTH_FIELDS, CAN_OFFSETS, strict=True)))
    self.assertEqual((self.probe["offsets"]["checksum"], self.probe["offsets"]["data"]), (5, 6))

  def test_compiled_health_and_can_goldens(self):
    self.assertEqual(bytes.fromhex(self.probe["current_health_hex"]), bytes.fromhex(CURRENT_HEX))
    self.assertEqual(bytes.fromhex(self.probe["can_health_hex"]), bytes.fromhex(CAN_HEX))
    self.assertEqual(bytes.fromhex(self.probe["can_frame_hex"]), bytes.fromhex("321809000023112233"))
    self.assertEqual(h.encode_health(current()), bytes.fromhex(self.probe["current_health_hex"]))
    self.assertEqual(h.encode_can_health(can_packet()), bytes.fromhex(self.probe["can_health_hex"]))

  def test_current_source_masks_and_byd_identity(self):
    self.assertEqual(self.probe["known_fault_mask"], h.KNOWN_FAULT_MASK)
    self.assertEqual(self.probe["known_alternative_experience_mask"], h.CURRENT_ALTERNATIVE_EXPERIENCE_MASK)
    self.assertEqual(self.probe["current_byd_safety"], 35)
    self.assertNotIn(32, h.CURRENT_SAFETY_MODES)

  def test_float32_quantization_matches_host_c_expression(self):
    self.assertGreaterEqual(len(self.probe["quantization_vectors"]), 6)
    for case in self.probe["quantization_vectors"]:
      actual = h.quantize_current_measurements(voltage_mv=0, current_ma=0, interrupt_load=case["load"], temperature_c=case["temperature"])
      self.assertEqual(actual["interrupt_load"], case["encoded_load"])
      self.assertEqual(actual["temperature"], case["encoded_temperature"])


if __name__ == "__main__":
  unittest.main()
