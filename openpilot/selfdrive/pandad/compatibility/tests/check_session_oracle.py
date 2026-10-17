#!/usr/bin/env python3
"""Compare native P3 results with P0 bindings and an independent wire formula.

The native test binary only uses fake transport backends. This program loads the
original P0 profile validator/registry and never imports a Panda/device API.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from compatibility.profile import load_profile
from compatibility.registry import register
from check_can_oracle import wire
from test_profile import observations, synthetic_dict


def json_value(value):
  return json.loads(json.dumps(value))


def fixture_profile(dos_slot, same_model):
  data = synthetic_dict(dos_slot)
  if same_model:
    for slot in data["slots"]:
      slot["expected_board_type"] = "RED_PANDA"
      slot["mcu_family"] = "H7"
      slot["exact_mcu"] = "fixture:unknown-H7"
  return load_profile(json.dumps(data))


def main():
  if not __debug__:
    raise RuntimeError("Oracle assertions must be enabled; do not use Python -O")
  parser = argparse.ArgumentParser()
  parser.add_argument("report", type=Path)
  args = parser.parse_args()
  report = json.loads(args.report.read_text())
  assert report["device_access"] is False
  assert report["real_can_tx"] is False
  assert report["scope"] == "synthetic_session_routes"
  assert len(report["fixtures"]) == 8
  seen = set()
  checks = 0
  for actual in report["fixtures"]:
    key = (actual["dos_slot"], actual["same_model"], actual["reversed_inventory"])
    assert type(key[0]) is int and key[0] in (0, 1)
    assert type(key[1]) is bool and type(key[2]) is bool
    assert key not in seen
    seen.add(key)
    profile = fixture_profile(*key[:2])
    supplied = observations(profile)[::-1] if key[2] else observations(profile)
    expected = register(profile, supplied)
    assert actual["profile"] == json_value(asdict(profile)), key
    assert actual["inventory"] == json_value([asdict(item) for item in supplied]), key
    assert actual["registered_identities"] == [item.identity for item in expected.boards], key
    assert actual["available"] is True, key
    assert actual["permissions"] == {"device_access_enabled": expected.device_access_enabled,
                                     "can_tx_enabled": expected.can_tx_enabled,
                                     "firmware_install_enabled": expected.firmware_install_enabled}, key
    expected_roles = {name: [item.identity for item in expected.for_role(name)]
                      for name in profile.roles.__dataclass_fields__}
    assert actual["role_identities"] == expected_roles, key
    checks += 6
    assert len(actual["routes"]) == 6
    for route, source in zip(actual["routes"], (0, 1, 2, 4, 5, 6), strict=True):
      slot, local_bus = divmod(source, 4)
      frame = {"address": 0x321 + source, "local_bus": local_bus,
               "extended": False, "fd": False, "returned": False,
               "rejected": False, "payload_hex": bytes((source, local_bus, slot)).hex()}
      expected_route = {"host_source": source, "slot": slot, "local_bus": local_bus,
                        "identity": expected.boards[slot].identity,
                        "wire_hex": wire(frame).hex(),
                        "rx_sources": [source, source + 0x80, source + 0xC0]}
      assert route == expected_route, (key, route, expected_route)
      checks += 1
  assert seen == {(d, s, r) for d in (0, 1) for s in (False, True) for r in (False, True)}
  print(json.dumps({"status": "PASS", "oracle": "P0 profile/registry plus independent CAN bit formula",
                    "fixture_count": len(seen), "route_checks": 6 * len(seen),
                    "cross_language_checks": checks, "checks": checks, "native_invocations": 1,
                    "assertions_enabled": __debug__, "device_access": False,
                    "real_can_tx": False, "scope": "synthetic_session_routes"}))


if __name__ == "__main__":
  main()
