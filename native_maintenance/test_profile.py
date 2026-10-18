"""The shipped review template cannot silently become a real binding."""
import json
from pathlib import Path
import unittest

class UnboundProfileTests(unittest.TestCase):
  def setUp(self):
    self.profile = json.loads((Path(__file__).parent / "profiles/unbound.json").read_text())

  def test_template_has_no_hardware_permissions(self):
    self.assertEqual(self.profile["schema"], "songplus-native-binding/v1")
    self.assertEqual(self.profile["binding_status"], "UNBOUND")
    for name in ("device_access_enabled", "can_tx_enabled", "firmware_install_enabled"):
      self.assertIs(self.profile[name], False)

  def test_both_slots_have_no_invented_identity(self):
    self.assertEqual([s["slot"] for s in self.profile["slots"]], [0, 1])
    for slot in self.profile["slots"]:
      self.assertEqual(set(slot), {"slot", "expected", "transport", "exact_mcu", "capabilities", "physical_evidence_sha256", "spi_selector"})
      for name, value in slot.items():
        if name != "slot":
          self.assertIsNone(value)

  def test_roles_are_unassigned(self):
    roles = self.profile["roles"]
    self.assertEqual(roles["ignition_sources"], [])
    self.assertEqual(set(roles), {"vehicle_can", "diagnostic", "harness", "fan_control", "power_monitor", "ir_control", "ignition_sources"})
    for name, value in roles.items():
      if name != "ignition_sources":
        self.assertEqual(value, -1)
