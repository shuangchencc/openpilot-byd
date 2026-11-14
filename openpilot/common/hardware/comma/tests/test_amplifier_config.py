#!/usr/bin/env python3
"""Hardware-free checks of the amplifier register sequences and of who selects them.

Unlike test_amplifier.py this touches no I2C bus and no panda, so it is safe to run on any
device, including one whose internal panda must never be reset.
"""
import ast
import hashlib
import inspect
import unittest
from unittest import mock

from openpilot.common.hardware.comma import amplifier, hardware
from openpilot.common.hardware.comma.amplifier import CONFIG, TICI_CONFIG, Amplifier

# sha256 over repr of the (name, value, register, offset, mask) tuples.
# The flat list upstream 0.11.3 applies to every device (base + former tizi block), 56 entries.
UPSTREAM_0_11_3_SEQUENCE = "dc71145586c9e7f4dcb163ad96aa4a31f2c452cb49d84791cd5592a2bedb0e49"
# BASE_CONFIG + CONFIGS["tici"] of AGNOS 12.4's /usr/comma/sound/amplifier.py, 91 entries:
# what that OS writes at boot on a comma three.
AGNOS_12_4_TICI_SEQUENCE = "2fdf0ed83f10c98284ec51afc5ca501ff2847ee96481555e420553a49875f8ae"


def digest(configs):
  return hashlib.sha256(repr([tuple(c) for c in configs]).encode()).hexdigest()


def final_state(configs):
  """Register bits a sequence leaves set, starting from zero. Registers never written are absent."""
  reg = {}
  for c in configs:
    reg[c.register] = (reg.get(c.register, 0) & ~c.mask & 0xFF) | ((c.value << c.offset) & c.mask)
  return reg


class FakeBus:
  """Stands in for SMBus: every register reads 0 until written, and writes are recorded in order."""
  writes: list[tuple[int, int]] = []

  def __init__(self, *args, **kwargs):
    self.reg = {}

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False

  def read_byte_data(self, addr, register, force=False):
    return self.reg.get(register, 0)

  def write_byte_data(self, addr, register, value, force=False):
    self.reg[register] = value
    FakeBus.writes.append((register, value))


class TestAmplifierConfig(unittest.TestCase):
  def test_config_is_what_upstream_applies(self):
    self.assertEqual(len(CONFIG), 56)
    self.assertEqual(digest(CONFIG), UPSTREAM_0_11_3_SEQUENCE)

  def test_tici_config_is_what_agnos_applies_at_boot(self):
    self.assertEqual(len(TICI_CONFIG), 91)
    self.assertEqual(digest(TICI_CONFIG), AGNOS_12_4_TICI_SEQUENCE)

  def test_tici_feeds_the_right_dac_from_the_left_slot(self):
    st = final_state(TICI_CONFIG)
    self.assertEqual(st[0x22], 0b00000010)         # right DAC <- DAI2 left, nothing else
    self.assertEqual(st[0x2C], 0x01)               # right speaker from right DAC
    self.assertEqual(st[0x3E] & 0x1F, 0x1C)        # right speaker volume
    self.assertNotIn(0x2B, st)                     # left speaker mixer untouched
    self.assertNotIn(0x3D, st)                     # left speaker volume untouched
    self.assertEqual(st[0x49] & 0b00000010, 0b10)  # DAI2 EQ on
    self.assertEqual(sorted(r for r in st if 0x84 <= r <= 0xB5), list(range(0x84, 0xB6)))  # five bands, ten registers each
    self.assertEqual(st[0x16] & 0b11, 0b11)        # DAI1 in/output enabled
    self.assertEqual(st[0x1E] & 0b11, 0b11)        # DAI2 in/output enabled

  def test_config_keeps_straight_stereo_mapping(self):
    st = final_state(CONFIG)
    self.assertEqual(st[0x22], 0b00100001)         # left DAC <- DAI2 left, right DAC <- DAI2 right
    self.assertEqual(st[0x3D] & 0x1F, 0x17)
    self.assertEqual(st[0x3E] & 0x1F, 0x17)
    self.assertEqual(st[0x49] & 0b00000010, 0)     # DAI2 EQ off

  def test_initialize_selects_the_sequence_by_model(self):
    cases = (("tici", TICI_CONFIG, 0b00000010), ("tizi", CONFIG, 0b00100001),
             (None, CONFIG, 0b00100001), ("something else", CONFIG, 0b00100001))
    for model, sequence, dac_mixer in cases:
      with self.subTest(model=model):
        FakeBus.writes = []
        with mock.patch.object(amplifier, "SMBus", FakeBus):
          ok = Amplifier().initialize_configuration(model) if model is not None else Amplifier().initialize_configuration()
        self.assertTrue(ok)
        writes = FakeBus.writes
        self.assertEqual(len(writes), 1 + len(sequence) + 1)
        self.assertEqual(writes[0], (0x51, 0x00))    # shut down first
        self.assertEqual(writes[-1], (0x51, 0x80))   # powered up last
        self.assertEqual([r for r, _ in writes[1:-1]], [c.register for c in sequence])
        self.assertEqual([v for r, v in writes if r == 0x22][-1], dac_mixer)

  def test_hardware_passes_its_device_type_at_every_call_site(self):
    # The wiring, not just the table. initialize_hardware() and set_power_save() cannot be run in
    # a test (they write sysfs through sudo), so read the calls off the source instead: a call
    # without the device type silently falls back to CONFIG, which is how the tici tuning was lost.
    tree = ast.parse(inspect.getsource(hardware))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "initialize_configuration"]
    self.assertGreaterEqual(len(calls), 2)
    for call in calls:
      with self.subTest(line=call.lineno):
        self.assertEqual([ast.unparse(a) for a in call.args], ["self.get_device_type()"])


if __name__ == "__main__":
  unittest.main()
