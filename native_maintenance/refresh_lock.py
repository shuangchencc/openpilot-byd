"""Explicit maintainer action only: freeze reviewed P9 files and test IDs.

Validation never invokes this module. It does not fetch, compile, or access any
device; callers must review the resulting diff and commit before acceptance.
"""
from __future__ import annotations

import argparse
import unittest

from maintenance import core as old
from maintenance import integration as parent
from native_maintenance.core import ROOT, LOCK, MANIFEST, SCOPE, protected_paths, write_json, read_json, sha


def test_ids():
  def flatten(suite):
    for item in suite:
      if isinstance(item, unittest.TestSuite):
        yield from flatten(item)
      else:
        yield item.id()
  return sorted(flatten(unittest.defaultTestLoader.discover(str(ROOT / "native_maintenance"), pattern="test_*.py")))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--write-reviewed-lock", action="store_true", required=True)
  parser.parse_args()
  parent.verify_lock(ROOT, metadata_only=True)
  previous = "results/songplus-integration-p8-full-002/validation/result.json"
  result = {"schema": "songplus-native-p9-lock/v1", "parent_lock_sha256": sha(ROOT / parent.LOCK),
            "baselines": read_json(ROOT / parent.LOCK)["baselines"], "scope": SCOPE,
            "series": old.normalized_series(ROOT, MANIFEST),
            "prerequisite": {"report": previous, "sha256": sha(ROOT / previous),
                             "maintenance_commit": read_json(ROOT / previous)["maintenance_commit"],
                             "reused_only": True, "newly_executed": False},
            "required_tests": test_ids(),
            "files": {name: sha(ROOT / name) for name in sorted(protected_paths())}}
  write_json(ROOT / LOCK, result)
  print("Frozen reviewed P9 lock: " + sha(ROOT / LOCK))


if __name__ == "__main__":
  main()
