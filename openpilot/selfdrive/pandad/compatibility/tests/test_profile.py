import ast
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from compatibility.profile import CURRENT_PROTOCOL, ProfileError, load_profile
from compatibility.registry import Observation, Registry, RegistryError, register
from compatibility.mock_transport import ScriptedTransport


PACKAGE = Path(__file__).resolve().parents[1]


def unbound_dict():
  return json.loads((PACKAGE / "profiles" / "unbound.json").read_text())


def synthetic_dict(dos_slot=0):
  data = unbound_dict()
  data["binding_status"] = "SYNTHETIC_BOUND"
  for slot in (0, 1):
    is_dos = slot == dos_slot
    data["slots"][slot].update({
      "identity_binding": "fixture:dos" if is_dos else "fixture:external",
      "expected_board_type": "DOS" if is_dos else "RED_PANDA",
      "mcu_family": "F4" if is_dos else "H7",
      "exact_mcu": "fixture:unknown-F4" if is_dos else "fixture:unknown-H7",
      "transport": "USB", "protocol_profile": CURRENT_PROTOCOL,
      "capabilities": {"can_buses": [0, 1, 2], "can_fd": not is_dos, "diagnostic": not is_dos,
                       "harness": not is_dos, "ignition": True, "fan": is_dos, "power_monitor": is_dos, "ir": False},
      "application_signature": "fixture:dos-app" if is_dos else "fixture:red-app",
      "safety_config_semantics": "noOutput",
    })
  data["roles"] = {"vehicle_can": 1 - dos_slot, "diagnostic": 1 - dos_slot, "harness": 1 - dos_slot,
                   "ignition_sources": [0, 1], "fan_control": dos_slot, "power_monitor": dos_slot, "ir_control": "UNSUPPORTED"}
  return data


def observations(profile):
  return tuple(Observation(s.identity_binding, s.expected_board_type, s.mcu_family, s.exact_mcu,
                           s.transport, s.protocol_profile, s.capabilities, s.application_signature) for s in profile.slots)


class TestProfile(unittest.TestCase):
  def test_unbound_loads_with_permissions_denied(self):
    profile = load_profile(json.dumps(unbound_dict()))
    self.assertEqual(profile.binding_status, "UNBOUND")
    self.assertFalse(profile.device_access_enabled or profile.can_tx_enabled or profile.firmware_install_enabled)
    with self.assertRaisesRegex(RegistryError, "UNBOUND_PROFILE"):
      register(profile, ())

  def test_profile_is_deeply_immutable(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    with self.assertRaises(FrozenInstanceError):
      profile.binding_status = "UNBOUND"
    with self.assertRaises(FrozenInstanceError):
      profile.slots[0].capabilities.can_fd = True
    self.assertIsInstance(profile.slots, tuple)
    self.assertIsInstance(profile.roles.ignition_sources, tuple)

  def test_duplicate_json_keys_rejected_at_every_depth(self):
    raw = json.dumps(unbound_dict())
    for old, new in (("\"schema\":", "\"schema\": 0, \"schema\":"),
                     ("\"slot\": 0", "\"slot\": 0, \"slot\": 0"),
                     ("\"vehicle_can\": null", "\"vehicle_can\": null, \"vehicle_can\": null")):
      with self.subTest(field=old), self.assertRaisesRegex(ProfileError, "duplicate JSON key"):
        load_profile(raw.replace(old, new, 1))

  def test_unknown_and_missing_fields_rejected(self):
    for mutation in (lambda d: d.update(extra=1), lambda d: d.pop("roles"), lambda d: d["slots"][0].pop("exact_mcu"),
                     lambda d: d["roles"].pop("diagnostic"), lambda d: d["slots"][0]["capabilities"].update(extra=False)):
      data = synthetic_dict()
      mutation(data)
      with self.subTest(mutation=mutation), self.assertRaises(ProfileError):
        load_profile(json.dumps(data))

  def test_permission_flags_cannot_be_enabled_or_coerced(self):
    for name in ("device_access_enabled", "can_tx_enabled", "firmware_install_enabled"):
      for value in (True, 0, "false", None):
        data = unbound_dict()
        data[name] = value
        with self.subTest(name=name, value=value), self.assertRaises(ProfileError):
          load_profile(json.dumps(data))

  def test_integer_fields_reject_boolean_and_coercion(self):
    for field, value in (("expected_board_count", 2.0), ("bus_stride", "4"), ("required_slots", [False, True]),
                         ("ordinary_local_can_buses", [False, 1, 2]), ("slots", {})):
      data = unbound_dict()
      data[field] = value
      with self.subTest(field=field), self.assertRaises(ProfileError):
        load_profile(json.dumps(data))
    data = synthetic_dict()
    data["slots"][0]["capabilities"]["fan"] = 1
    with self.assertRaises(ProfileError):
      load_profile(json.dumps(data))

  def test_nonfinite_invalid_encoding_oversize_and_deep_input(self):
    for raw in (b"\xff", "NaN", "Infinity", "{", " " * 32769, "[" * 2000 + "]" * 2000, 7):
      with self.subTest(raw_type=type(raw).__name__), self.assertRaises(ProfileError):
        load_profile(raw)

  def test_non_fixture_identities_and_legacy_abi_rejected(self):
    for key, value in (("identity_binding", "actual-serial"), ("identity_binding", ""), ("exact_mcu", "STM32F413"),
                       ("application_signature", "hardware-signature"), ("protocol_profile", "legacy:16/4/5"),
                       ("transport", "AUTO"), ("mcu_family", "UNKNOWN"), ("safety_config_semantics", "allOutput")):
      data = synthetic_dict()
      data["slots"][0][key] = value
      with self.subTest(key=key, value=value), self.assertRaises(ProfileError):
        load_profile(json.dumps(data))

  def test_unbound_partial_or_role_assignment_rejected(self):
    for change in (lambda d: d["slots"][0].update(identity_binding="fixture:x"),
                   lambda d: d["roles"].update(vehicle_can=0),
                   lambda d: d.update(binding_status="SYNTHETIC_BOUND")):
      data = unbound_dict()
      change(data)
      with self.assertRaises(ProfileError):
        load_profile(json.dumps(data))

  def test_unknown_board_type_cannot_self_authorize(self):
    for board_type in ("UNKNOWN", "SYNTHETIC_SAME_TYPE", "DOS_CLONE", "RED", ""):
      data = synthetic_dict()
      data["slots"][0]["expected_board_type"] = board_type
      with self.subTest(board_type=board_type), self.assertRaises(ProfileError):
        load_profile(json.dumps(data))

  def test_known_board_type_requires_its_mcu_family(self):
    for slot, family in ((0, "H7"), (1, "F4")):
      data = synthetic_dict()
      data["slots"][slot]["mcu_family"] = family
      data["slots"][slot]["capabilities"]["can_fd"] = False
      with self.subTest(slot=slot, family=family), self.assertRaisesRegex(ProfileError, "board/MCU family"):
        load_profile(json.dumps(data))

  def test_fixed_slots_and_duplicate_binding(self):
    for change in (lambda d: d["slots"].reverse(), lambda d: d["slots"][1].update(slot=0),
                   lambda d: d["slots"][1].update(identity_binding="fixture:dos")):
      data = synthetic_dict()
      change(data)
      with self.assertRaises(ProfileError):
        load_profile(json.dumps(data))

  def test_missing_wrong_or_ambiguous_roles_rejected(self):
    for name in ("vehicle_can", "diagnostic", "harness", "ignition_sources", "fan_control", "power_monitor", "ir_control"):
      data = synthetic_dict()
      data["roles"][name] = None
      with self.subTest(name=name), self.assertRaises(ProfileError):
        load_profile(json.dumps(data))
    for role, owner in (("vehicle_can", True), ("harness", 0), ("diagnostic", 0), ("fan_control", 1),
                        ("power_monitor", 1), ("ir_control", 0), ("ignition_sources", []), ("ignition_sources", [0, 0])):
      data = synthetic_dict()
      data["roles"][role] = owner
      with self.subTest(role=role, owner=owner), self.assertRaises(ProfileError):
        load_profile(json.dumps(data))

  def test_unsupported_peripheral_is_explicit_and_cannot_hide_capability(self):
    data = synthetic_dict()
    data["roles"]["fan_control"] = "UNSUPPORTED"
    with self.assertRaises(ProfileError):
      load_profile(json.dumps(data))
    data["slots"][0]["capabilities"]["fan"] = False
    profile = load_profile(json.dumps(data))
    self.assertEqual(register(profile, observations(profile)).for_role("fan_control"), ())

  def test_f4_fd_and_illegal_can_buses_rejected(self):
    for key, value in (("can_fd", True), ("can_buses", [0, 1, 2, 3]), ("can_buses", [0, 0, 2])):
      data = synthetic_dict()
      data["slots"][0]["capabilities"][key] = value
      with self.subTest(key=key), self.assertRaises(ProfileError):
        load_profile(json.dumps(data))

  def test_ignition_requires_owner_capability(self):
    data = synthetic_dict()
    data["slots"][0]["capabilities"]["ignition"] = False
    with self.assertRaises(ProfileError):
      load_profile(json.dumps(data))


class TestRegistry(unittest.TestCase):
  def test_both_role_arrangements_and_reverse_enumeration(self):
    for dos_slot in (0, 1):
      profile = load_profile(json.dumps(synthetic_dict(dos_slot)))
      expected = observations(profile)
      for discovered in (expected, expected[::-1]):
        with self.subTest(dos_slot=dos_slot, reverse=discovered != expected):
          group = register(profile, discovered)
          self.assertEqual(group.boards, expected)
          self.assertEqual(group.for_role("vehicle_can")[0].identity, "fixture:external")
          self.assertEqual(group.for_role("fan_control")[0].identity, "fixture:dos")
          self.assertEqual(group.for_role("ignition_sources"), expected)
          self.assertEqual(group.for_role("ir_control"), ())
          self.assertFalse(group.device_access_enabled or group.can_tx_enabled or group.firmware_install_enabled)

  def test_same_type_boards_are_bound_by_identity_only(self):
    data = synthetic_dict()
    for slot in data["slots"]:
      slot["expected_board_type"] = "RED_PANDA"
      slot["mcu_family"] = "H7"
      slot["exact_mcu"] = "fixture:unknown-H7"
    profile = load_profile(json.dumps(data))
    observed = observations(profile)
    self.assertEqual(register(profile, observed[::-1]).boards, observed)

  def test_empty_missing_and_extra_inventory_fail_entire_group(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    observed = observations(profile)
    for inventory in ((), observed[:1], observed + (replace(observed[1], identity="fixture:extra"),)):
      with self.subTest(count=len(inventory)), self.assertRaisesRegex(RegistryError, "REQUIRED_BOARD_COUNT"):
        register(profile, inventory)

  def test_duplicate_and_dual_transport_identity_quarantined(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    observed = observations(profile)
    for second in (observed[0], replace(observed[0], transport="SPI")):
      with self.assertRaisesRegex(RegistryError, "DUPLICATE_IDENTITY"):
        register(profile, (observed[0], second))

  def test_unknown_identity_and_replacement_rejected(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    observed = observations(profile)
    with self.assertRaisesRegex(RegistryError, "UNEXPECTED_IDENTITY"):
      register(profile, (observed[0], replace(observed[1], identity="fixture:replacement")))

  def test_observation_mismatches_rejected(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    observed = observations(profile)
    for field, value in (("transport", "SPI"), ("exact_mcu", "fixture:other"),
                         ("application_signature", "fixture:other-app"), ("capabilities", replace(observed[0].capabilities, fan=False))):
      with self.subTest(field=field), self.assertRaisesRegex(RegistryError, f"MISMATCH_{field.upper()}"):
        register(profile, (replace(observed[0], **{field: value}), observed[1]))
    # A valid catalog entry still cannot replace a different expected board.
    wrong_board = replace(observed[0], board_type="RED_PANDA", mcu_family="H7", exact_mcu="fixture:unknown-H7")
    with self.assertRaisesRegex(RegistryError, "MISMATCH_BOARD_TYPE"):
      register(profile, (wrong_board, observed[1]))

  def test_invalid_observation_and_profile_types_fail_closed(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    for supplied_profile, inventory in ((None, ()), (profile, []), (profile, ({}, {}))):
      with self.assertRaises(RegistryError):
        register(supplied_profile, inventory)

  def test_direct_registry_construction_cannot_bypass_checks(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    observed = observations(profile)
    with self.assertRaisesRegex(RegistryError, "NONCANONICAL_REGISTRY"):
      Registry(profile, observed[::-1])
    with self.assertRaisesRegex(RegistryError, "REQUIRED_BOARD_COUNT"):
      Registry(profile, observed[:1])

  def test_unsupported_role_name_rejected(self):
    profile = load_profile(json.dumps(synthetic_dict()))
    group = register(profile, observations(profile))
    for name in ("default", "reset", "__dict__", None):
      with self.assertRaisesRegex(RegistryError, "UNKNOWN_ROLE"):
        group.for_role(name)

  def test_construction_registration_and_listing_have_no_hardware_calls(self):
    raw = json.dumps(synthetic_dict())
    targets = ("builtins.open", "os.open", "socket.socket", "subprocess.Popen", "pathlib.Path.write_text")
    spies = []
    from contextlib import ExitStack
    with ExitStack() as stack:
      for target in targets:
        spies.append(stack.enter_context(patch(target, side_effect=AssertionError("unexpected IO"))))
      profile = load_profile(raw)
      group = register(profile, observations(profile)[::-1])
      self.assertEqual(len(group.boards), 2)
      transport = ScriptedTransport(())
      self.assertEqual(transport.trace_json(), [])
      transport.close()
    for spy in spies:
      spy.assert_not_called()

  def test_imports_have_no_hardware_or_runtime_dependency(self):
    allowed = {"dataclasses", "json", "profile"}
    for name in ("__init__.py", "profile.py", "registry.py", "mock_transport.py"):
      tree = ast.parse((PACKAGE / name).read_text())
      for node in ast.walk(tree):
        if isinstance(node, ast.Import):
          self.assertTrue(all(alias.name in allowed for alias in node.names), name)
        elif isinstance(node, ast.ImportFrom):
          self.assertIn(node.module, allowed, name)


if __name__ == "__main__":
  unittest.main()
