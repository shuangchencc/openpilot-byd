#!/usr/bin/env python3
"""Maintain the pinned P0–P7 offline candidate. No installer or hardware entry."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance.core import read_json, replay, verify_lock


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest='command', required=True)
  check = commands.add_parser('verify-lock', help='Check pins/hashes/cached dependencies without fetching')
  check.add_argument('--metadata-only', action='store_true', help='Versioned contract only; no build-ready claim')
  replay_parser = commands.add_parser('replay', help='Reconstruct declared sources and deterministic source archive')
  replay_parser.add_argument('--output', type=Path, required=True)
  validate = commands.add_parser('validate', help='Fresh offline P5/P6, second ARM builds, P7 gates')
  validate.add_argument('--output', type=Path, required=True)
  validate.add_argument('--linux-instance', required=True, help='Already-running local Lima VM')
  validate.add_argument('--linux-root', required=True, help='Prepared Linux dependency directory')
  release = commands.add_parser('release-check', help='Verify evidence; return 3 for valid offline acceptance with deployment blocked')
  release.add_argument('--report', type=Path, required=True)
  inspect = commands.add_parser('inspect-runtime', help='Inspect supplied synthetic inventory and files only; never launches runtime')
  inspect.add_argument('--inventory', type=Path, required=True)
  inspect.add_argument('--manifest', type=Path, required=True)
  inspect.add_argument('--artifact-root', type=Path, required=True)
  args = parser.parse_args()
  if args.command == 'verify-lock':
    result = verify_lock(metadata_only=args.metadata_only)
  elif args.command == 'replay':
    result = replay(args.output)
    result = {k: v for k, v in result.items() if k != 'files'}
  elif args.command == 'validate':
    from maintenance.validation import validate
    result = validate(args.output, args.linux_instance, args.linux_root)
    result = {'status': result['status'], 'result': str(args.output.resolve() / 'result.json'), **result['release']}
  elif args.command == 'release-check':
    from maintenance.validation import release_check
    result = release_check(args.report)
  else:
    from maintenance.runtime_guard import inspect_runtime
    verify_lock(metadata_only=True)
    result = inspect_runtime(read_json(args.inventory), read_json(args.manifest), args.artifact_root)
  print(json.dumps(result, indent=2, ensure_ascii=False))
  if args.command == 'release-check':
    return 3 if result['offline_compatibility_complete'] else 2
  if args.command == 'inspect-runtime' and not result['inspection_valid']:
    return 2
  return 0


if __name__ == '__main__':
  try:
    sys.exit(main())
  except (ValueError, KeyError, OSError, TypeError, subprocess.SubprocessError) as error:
    print(json.dumps({'status': 'FAIL', 'error': str(error), 'deployment_ready': False, 'device_access': False}))
    sys.exit(2)
