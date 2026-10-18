#!/usr/bin/env python3
"""Replay P5+P8 and verify ARM application resources without network/devices.

Uses the locked cached macOS compiler and Seatbelt sandbox. No bootstub, MCU
execution, hardware enumeration, flashing, or installation target exists.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/panda/p8'


def sha(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root):
  result = {}
  for path in sorted(root.rglob('*')):
    if '.git' in path.relative_to(root).parts:
      continue
    if path.is_symlink():
      raise ValueError('source symlink forbidden: ' + str(path))
    if path.is_file():
      result[str(path.relative_to(root))] = sha(path)
  return result


def capture(argv, cwd=ROOT):
  return subprocess.check_output(list(map(str, argv)), cwd=cwd)


def new_output(path):
  path = Path(os.path.abspath(path))
  if path.exists() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
    raise ValueError('output must be new and have no symlink ancestors: ' + str(path))
  path.mkdir(parents=True)
  return path


def archive(repo, commit, destination):
  destination.mkdir(parents=True)
  with tarfile.open(fileobj=io.BytesIO(capture(['git', 'archive', commit], repo))) as tar:
    for member in tar:
      path = destination / member.name
      if not path.resolve().is_relative_to(destination.resolve()) or not (member.isdir() or member.isfile()):
        raise ValueError('unexpected archive member')
      if member.isdir():
        path.mkdir(parents=True, exist_ok=True)
      else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tar.extractfile(member).read())


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--output', type=Path)
  parser.add_argument('--python', type=Path, default=ROOT / 'upstream-openpilot/.venv/bin/python')
  args = parser.parse_args()
  if platform.system() != 'Darwin' or sys.flags.optimize:
    raise RuntimeError('formal runner requires macOS Seatbelt and Python without -O')
  parent = ROOT / 'results/songplus-integration-p8-resources'
  if args.output is None:
    out = new_output(parent / ('run-' + uuid.uuid4().hex))
  else:
    out = new_output(args.output)
  report = {'schema': 'songplus-p8-resources-offline/v1', 'status': 'RUNNING',
            'utc': datetime.now(timezone.utc).isoformat(), 'commands': [], 'reports': {},
            'device_access': False, 'mcu_executed': False, 'bootstub_built': False,
            'deployable': False, 'runtime_resource_gate': 'BLOCKED'}
  def verify_inputs(items):
    for name, expected in items.items():
      if sha(ROOT / name) != expected:
        raise RuntimeError('locked input changed: ' + name)
  try:
    manifest_path = PATCH / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    parent_path = ROOT / manifest['parent_manifest']
    if sha(parent_path) != manifest['parent_manifest_sha256']:
      raise RuntimeError('parent manifest hash mismatch')
    ancestor = json.loads(parent_path.read_text())
    lock = json.loads((ROOT / 'upstream-lock.json').read_text())
    pins = {r['path']: r['commit'] for r in lock['repos']}
    # Preserve the venv executable path: resolving its symlink selects the base
    # interpreter without the locked venv's cffi/capnp dependencies.
    python = args.python.absolute()
    if capture([python, '--version']).decode().strip() != 'Python ' + lock['python_dependencies']['python_version']:
      raise RuntimeError('Python version mismatch')
    for path, commit in manifest['baselines'].items():
      if pins[path] != commit or capture(['git', 'rev-parse', 'HEAD'], ROOT / path).decode().strip() != commit:
        raise RuntimeError('baseline mismatch: ' + path)
    locked_inputs = {**ancestor['baseline_inputs'], **ancestor['dependencies'], **manifest['validator_inputs']}
    verify_inputs(locked_inputs)
    report['source_commit'] = manifest['series'][-1]['commit']
    report['manifest_sha256'] = sha(manifest_path)
    report['parent_manifest_sha256'] = sha(parent_path)
    report['baselines'] = manifest['baselines']
    report['locked_input_count'] = len(locked_inputs)
    report['sandbox'] = 'macos-seatbelt: network, device files and IOKit denied'
    sandbox = ['/usr/bin/sandbox-exec', '-f', str(ROOT / 'tools/panda_firmware_offline.sb')]
    temporary = out / 'tmp'
    temporary.mkdir()
    env = dict(os.environ, PYTHONOPTIMIZE='0', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
               UV_OFFLINE='1', PIP_NO_INDEX='1', ASAN_OPTIONS='detect_leaks=0:halt_on_error=1',
               UBSAN_OPTIONS='halt_on_error=1', TMPDIR=str(temporary))
    def run(name, argv):
      command = sandbox + list(map(str, argv))
      proc = subprocess.run(command, cwd=out, env=env, capture_output=True, timeout=1200)
      log = out / (name + '.log')
      log.write_bytes(proc.stdout + proc.stderr)
      report['commands'].append({'name': name, 'argv': command, 'exit_code': proc.returncode,
                                 'log': log.name, 'log_sha256': sha(log)})
      if proc.returncode:
        raise RuntimeError(f'{name} failed ({proc.returncode}); see {log}')
    trees = []
    for repetition in ('first', 'second'):
      panda, dbc = out / repetition / 'panda', out / repetition / 'opendbc'
      archive(ROOT / 'upstream-openpilot/panda', manifest['baselines']['upstream-openpilot/panda'], panda)
      archive(ROOT / 'upstream-openpilot/opendbc_repo', manifest['baselines']['upstream-openpilot/opendbc_repo'], dbc)
      capture(['git', 'init', '-q'], panda)
      for patchroot, series, expected in ((parent_path.parent, ancestor['series'], ancestor['result_tree']),
                                          (PATCH, manifest['series'], manifest['result_tree'])):
        for item in series:
          patch = (patchroot / item['file']).resolve()
          if not patch.is_relative_to(ROOT / 'patches') or sha(patch) != item['sha256']:
            raise RuntimeError('patch identity mismatch')
          capture(['git', 'apply', '--check', patch], panda)
          capture(['git', 'apply', patch], panda)
        if tree_hashes(panda) != expected:
          raise RuntimeError('replayed source tree mismatch')
      trees.append((panda, dbc, tree_hashes(dbc)))
      for target in ('f4', 'h7'):
        for policy in ('debug', 'release'):
          name = target + '-' + policy
          build = out / repetition / name
          run('build-' + repetition + '-' + name, [python, panda / 'tools/p5/build_candidate.py',
              '--source', panda, '--opendbc', dbc, '--arm-prefix', args.python.absolute().parent,
              '--output', build, '--target', target, '--policy', policy])
          run('audit-' + repetition + '-' + name, [python, panda / 'tests/p5/audit_resources.py',
              '--target', target, '--elf', build / 'main.elf', '--map', build / 'main.map',
              '--raw', build / 'main.bin', '--signed', build / 'main.bin.signed',
              '--public-key', panda / 'board/crypto/certs/debug.pub', '--stack-dir', build,
              '--output', build / 'resources.json'])
          report['reports'][repetition + '-' + name] = {
            'build': json.loads((build / 'build.json').read_text()),
            'resources': json.loads((build / 'resources.json').read_text())}
    comparisons = []
    for target in ('f4', 'h7'):
      for policy in ('debug', 'release'):
        for artifact in ('main.bin', 'main.bin.signed', 'main.elf'):
          relative = Path(target + '-' + policy) / artifact
          left, right = sha(out / 'first' / relative), sha(out / 'second' / relative)
          if left != right:
            raise RuntimeError('independent source/output build mismatch: ' + str(relative))
          comparisons.append({'artifact': str(relative), 'sha256': left, 'byte_identical': True})
    report['reproducibility'] = comparisons
    panda, dbc, _ = trees[0]
    jobs = [
      ('resource-faults', 'tests/p8/run_resources.py', ['--source', panda, '--arm-prefix', args.python.absolute().parent,
          '--f4-build', out / 'first/f4-debug', '--h7-build', out / 'first/h7-debug'], 'result.json'),
      ('protocol', 'tests/p5/run_validation.py', ['--panda', panda, '--opendbc', dbc, '--arm-prefix', args.python.absolute().parent], 'validation.json'),
      ('bxcan', 'tests/p5/run_bxcan.py', ['--source', panda, '--opendbc', dbc], 'result.json'),
      ('fdcan', 'tests/p5/run_fdcan.py', ['--source', panda, '--opendbc', dbc], 'result.json'),
      ('bsp', 'tests/f4_bsp/run_bsp.py', ['--source', panda, '--opendbc', dbc, '--arm-prefix', args.python.absolute().parent], 'result.json'),
      ('capabilities', 'tests/p5/run_capabilities.py', ['--panda', panda], 'result.json'),
      ('safety', 'tests/p5/run_safety.py', ['--opendbc', dbc], 'safety.json'),
      *[(f'liveness-{target}', 'tests/p5/run_firmware_liveness.py',
         ['--panda', panda, '--opendbc', dbc, '--target', target], 'result.json') for target in ('f4', 'h7')],
    ]
    for name, script, flags, filename in jobs:
      destination = out / name
      run(name, [python, panda / script, *flags, '--output', destination])
      data = json.loads((destination / filename).read_text())
      if not data['status'].startswith('PASS') or any(data.get(key) for key in ('failed', 'failures', 'errors', 'skipped')):
        raise RuntimeError('incomplete regression: ' + name)
      report['reports'][name] = data
    r = report['reports']
    assert r['resource-faults']['passed'] == 23
    assert len(r['protocol']['abi']) == 2 and sum(len(a['negative_compile_cases']) for a in r['protocol']['abi']) == 6
    assert [a['groups'] for a in r['protocol']['protocol']] == [15, 15]
    assert r['bxcan']['groups'] == 24 and r['fdcan']['groups'] == 14 and r['bsp']['summary']['groups'] == 23
    assert r['capabilities']['tests_run'] == 12 and r['safety']['tests_run'] == len(set(r['safety']['selectors'])) == 49
    assert all(r['liveness-' + target]['summary']['passed'] == 13 for target in ('f4', 'h7'))
    report['generated_safety_objects'] = {}
    for panda, dbc, expected_dbc in trees:
      actual_dbc = tree_hashes(dbc)
      if tree_hashes(panda) != manifest['result_tree'] or any(actual_dbc.get(p) != h for p, h in expected_dbc.items()):
        raise RuntimeError('worker modified source')
      # Pinned upstream libsafety explicitly puts mkstemp .os files beside
      # safety.c. Accept only those added compiler objects in this throwaway
      # archive; every original source still must match its pre-test hash.
      for name in actual_dbc.keys() - expected_dbc.keys():
        if not Path(name).match('opendbc/safety/tests/libsafety/tmp*.os'):
          raise RuntimeError('unexpected generated source artifact: ' + name)
        report['generated_safety_objects'][str((dbc / name).relative_to(out))] = actual_dbc[name]
    verify_inputs(locked_inputs)
    if sha(manifest_path) != report['manifest_sha256'] or sha(parent_path) != report['parent_manifest_sha256']:
      raise RuntimeError('manifest changed during validation')
    report['status'] = 'PASS_OFFLINE_CANDIDATE'
    report['summary'] = {'arm_builds': 8, 'resource_audits': 8, 'identical_artifact_pairs': 12,
                         'resource_checks': 23, 'protocol_groups': 30, 'protocol_negative_abi_compiles': 6,
                         'bxcan_groups': 24, 'fdcan_groups': 14, 'bsp_groups': 23,
                         'capability_tests': 12, 'safety_tests': 49, 'liveness_groups': 26,
                         'failed': 0, 'skipped': 0, 'runtime_resource_proof_passed': False}
  except Exception as exc:
    report['status'] = 'FAIL'
    report['error'] = f'{type(exc).__name__}: {exc}'
  (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
  print(json.dumps({'status': report['status'], 'result': str(out / 'result.json'),
                    'runtime_resource_gate': 'BLOCKED', **({'error': report['error']} if 'error' in report else {})}))
  return int(report['status'] != 'PASS_OFFLINE_CANDIDATE')


if __name__ == '__main__':
  raise SystemExit(main())
