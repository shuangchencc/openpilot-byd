#!/usr/bin/env python3
"""P8 receive-only integration maintenance on the frozen P0-P7 contract."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance import integration


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest="command", required=True)
  verify = commands.add_parser("verify-lock", help="Verify P7 and additive integration locks without fetching")
  verify.add_argument("--metadata-only", action="store_true")
  replay = commands.add_parser("replay", help="Replay both patch layers and package receive-only sources")
  replay.add_argument("--output", type=Path, required=True)
  validate = commands.add_parser("validate", help="Execute fresh P0-P7, vehicle, runtime and resource acceptance")
  validate.add_argument("--output", type=Path, required=True)
  validate.add_argument("--linux-instance", required=True)
  validate.add_argument("--linux-root", required=True)
  release = commands.add_parser("release-check", help="Verify offline evidence; valid blocked candidate exits 3")
  release.add_argument("--report", type=Path, required=True)
  args = parser.parse_args()
  if args.command == "verify-lock":
    result = integration.verify_lock(metadata_only=args.metadata_only)
  elif args.command == "replay":
    result = integration.replay(args.output)
    result = {k: v for k, v in result.items() if k != "files"}
  elif args.command == "validate":
    result = integration.validate(args.output, args.linux_instance, args.linux_root)
    result = {"status": result["status"], "result": str(args.output.resolve() / "result.json"),
              "deployment_ready": False, "device_access": False}
  else:
    result = integration.release_check(args.report)
  print(json.dumps(result, indent=2, ensure_ascii=False))
  return 3 if args.command == "release-check" else 0


if __name__ == "__main__":
  try:
    sys.exit(main())
  except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
    print(json.dumps({"status": "FAIL", "error": str(error), "deployment_ready": False, "device_access": False}))
    sys.exit(2)
