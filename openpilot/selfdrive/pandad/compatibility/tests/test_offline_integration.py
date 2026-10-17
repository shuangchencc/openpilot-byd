"""Compose the pure P0/P1 modules without importing the active pandad package."""
import json
import os
from pathlib import Path
import unittest

from compatibility import health
from compatibility.mock_transport import ScriptStep, ScriptedTransport
from compatibility.profile import CURRENT_PROTOCOL, ProfileError, load_profile
from compatibility.registry import register
from test_profile import observations, synthetic_dict


class OfflineIntegration(unittest.TestCase):
  def test_profile_and_version_identity_agree(self):
    self.assertEqual(CURRENT_PROTOCOL, "current:" + health.encode_versions(health.CURRENT_VERSION).hex())
    for slot in (0, 1):
      profile = load_profile(json.dumps(synthetic_dict(slot)))
      registry = register(profile, tuple(reversed(observations(profile))))
      self.assertEqual(registry.for_role("vehicle_can")[0].board_type, "RED_PANDA")
      self.assertFalse(registry.can_tx_enabled)

  def test_valid_mock_protocol_read_never_enables_control(self):
    fixture = json.loads(Path(os.environ["TEST_HEALTH_LAYOUT_JSON"]).read_text())
    profile = load_profile(json.dumps(synthetic_dict()))
    registry = register(profile, observations(profile))
    transport = ScriptedTransport((ScriptStep("read", health.CURRENT_VERSION_BYTES),
                                   ScriptStep("read", bytes.fromhex(fixture["current_health_hex"]))))
    version = health.decode_versions(transport.read(8))
    packet = health.decode_health(transport.read(48), version)
    view = health.normalize_health(packet, health.HealthCapabilities())
    self.assertTrue(view.control_protocol_supported)
    self.assertIsNone(view.values["temperature_c"])
    self.assertFalse(registry.can_tx_enabled)
    self.assertFalse(registry.device_access_enabled)
    self.assertFalse(registry.firmware_install_enabled)
    self.assertEqual([event.operation for event in transport.trace], ["read", "read"])
    self.assertTrue(all(not event.actual_device_access and not event.actual_can_tx for event in transport.trace))

  def test_bad_or_legacy_version_never_consumes_reset_or_fallback(self):
    for reply in (b"", b"\x00" * 7, health.LEGACY_VERSION_BYTES, b"\x00" * 8, b"\x00" * 9):
      transport = ScriptedTransport((ScriptStep("read", reply), ScriptStep("write", b"forbidden reset intent")))
      with self.assertRaises(health.HealthCodecError):
        health.decode_versions(transport.read(max(8, len(reply))))
      self.assertEqual(transport.remaining_steps, 1)
      self.assertEqual([event.operation for event in transport.trace], ["read"])

  def test_legacy_offline_decode_cannot_be_registered(self):
    version = health.decode_versions(health.LEGACY_VERSION_BYTES, profile=health.ProtocolProfile.SP098_OFFLINE)
    self.assertFalse(version.control_protocol_supported)
    data = synthetic_dict()
    data["slots"][0]["protocol_profile"] = "legacy:" + health.encode_versions(version).hex()
    with self.assertRaises(ProfileError):
      load_profile(json.dumps(data))
