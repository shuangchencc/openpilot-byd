#!/usr/bin/env python3
"""Replay pinned P5 Panda and P0-P4 host patches; build/test offline app candidates.

macOS seatbelt denies network and device files to every compiler/test child.
No dependency fetch, device constructor, bootstub build or installation target.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/panda/p5'


def sha(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(argv, cwd=ROOT):
  return subprocess.check_output(list(map(str, argv)), cwd=cwd)


def archive(repo, commit, dest):
  data = capture(['git', 'archive', commit], repo)
  dest.mkdir(parents=True)
  with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    for member in tar:
      path = dest / member.name
      assert path.resolve().is_relative_to(dest.resolve()) and not member.issym() and not member.islnk()
      if member.isdir():
        path.mkdir(parents=True, exist_ok=True)
      else:
        assert member.isfile()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tar.extractfile(member).read())


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--python', type=Path, default=ROOT / 'upstream-openpilot/.venv/bin/python')
  args = parser.parse_args()
  assert platform.system() == 'Darwin', 'Formal runner requires the declared macOS seatbelt environment'
  assert sys.flags.optimize == 0
  parent = ROOT / 'results/panda-firmware-p5'
  parent.mkdir(parents=True, exist_ok=True)
  out = Path(tempfile.mkdtemp(prefix='run-', dir=parent))
  (parent / 'latest.json').write_text(json.dumps({'status': 'RUNNING', 'result': str(out / 'result.json')})+'\n')
  manifest = json.loads((PATCH / 'manifest.json').read_text())
  lock = json.loads((ROOT / 'upstream-lock.json').read_text())
  pins = {r['path']: r['commit'] for r in lock['repos']}
  python = args.python.absolute()
  assert capture([python, '--version']).decode().strip() == 'Python ' + lock['python_dependencies']['python_version']
  for path, commit in manifest['baselines'].items():
    assert pins[path] == commit == capture(['git', 'rev-parse', 'HEAD'], ROOT / path).decode().strip()
  for path, digest in manifest['baseline_inputs'].items():
    assert sha(ROOT / path) == digest, path
  for path, digest in manifest['dependencies'].items():
    assert sha(ROOT / path) == digest, path
  hostpath = ROOT / 'patches/openpilot/p4/manifest.json'
  assert sha(hostpath) == manifest['host_p0_p4_manifest_sha256']
  hostmanifest = json.loads(hostpath.read_text())
  panda, dbc, host = out / 'panda', out / 'opendbc', out / 'host'
  archive(ROOT / 'upstream-openpilot/panda', manifest['baselines']['upstream-openpilot/panda'], panda)
  archive(ROOT / 'upstream-openpilot/opendbc_repo', manifest['baselines']['upstream-openpilot/opendbc_repo'], dbc)
  # Reconstruct the P4 exact staged tree from its pinned file manifest, not a working checkout.
  host.mkdir()
  for path, digest in hostmanifest['baseline_inputs'].items():
    origin = ROOT / path
    assert sha(origin) == digest
    target = host / path.removeprefix('upstream-openpilot/')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(origin.read_bytes())
  for tree, patchroot, series in ((panda, PATCH, manifest['series']), (host, hostpath.parent, hostmanifest['series'])):
    capture(['git', 'init', '-q'], tree)
    for patch in series:
      file = (patchroot / patch['file']).resolve()
      assert file.is_relative_to(ROOT / 'patches') and sha(file) == patch['sha256']
      capture(['git', 'apply', '--check', file], tree)
      capture(['git', 'apply', file], tree)
  for path, digest in manifest['result_files'].items():
    assert sha(panda / path) == digest, path
  for path, digest in hostmanifest['result_files'].items():
    assert sha(host / path) == digest, path
  # Verify the entire replayed Panda source, including untouched files, before generated output.
  actual = {str(p.relative_to(panda)): sha(p) for p in panda.rglob('*') if p.is_file() and '.git' not in p.relative_to(panda).parts}
  assert actual == manifest['result_tree'], 'Panda replay tree mismatch'
  dbc_inputs = {str(p.relative_to(dbc)): sha(p) for p in dbc.rglob('*') if p.is_file()}
  sandbox = ['/usr/bin/sandbox-exec', '-f', str(ROOT / 'tools/panda_firmware_offline.sb')]
  env = dict(os.environ, PYTHONOPTIMIZE='0', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
             UV_OFFLINE='1', PIP_NO_INDEX='1', P2_NETWORK_DEVICE_SANDBOX='macos-seatbelt',
             ASAN_OPTIONS='detect_leaks=0:halt_on_error=1', UBSAN_OPTIONS='halt_on_error=1',
             PYTHONPATH=str(host / 'openpilot/selfdrive/pandad'), SONGPLUS_SCHEMA_ROOT=str(host / 'openpilot/cereal'),
             SONGPLUS_CAR_SCHEMA_ROOT=str(host / 'opendbc_repo/opendbc/car'))
  commands = []
  def run(name, argv):
    full = sandbox + list(map(str, argv))
    r = subprocess.run(full, cwd=out, env=env, capture_output=True, timeout=1200, check=False)
    (out / (name + '.log')).write_bytes(r.stdout + r.stderr)
    commands.append({'name': name, 'command': full, 'exit_code': r.returncode, 'log_sha256': sha(out / (name + '.log'))})
    if r.returncode:
      raise RuntimeError(f'{name}: see {out / (name + ".log")}\n{r.stdout.decode()[-1000:]}{r.stderr.decode()[-1500:]}')
    return r.stdout.decode()
  reports = {}
  for target in ('f4', 'h7'):
    for policy in ('debug', 'release'):
      name = target + '-' + policy
      build = out / name
      run('build-' + name, [python, panda / 'tools/p5/build_candidate.py', '--source', panda, '--opendbc', dbc,
          '--arm-prefix', python.parent, '--output', build, '--target', target, '--policy', policy])
      run('resources-' + name, [python, panda / 'tests/p5/audit_resources.py', '--target', target,
          '--elf', build / 'main.elf', '--map', build / 'main.map', '--raw', build / 'main.bin', '--signed', build / 'main.bin.signed',
          '--public-key', panda / 'board/crypto/certs/debug.pub', '--stack-dir', build, '--output', build / 'resources.json'])
      reports[name] = {'build': json.loads((build / 'build.json').read_text()), 'resources': json.loads((build / 'resources.json').read_text())}
  jobs = [
    ('protocol', 'tests/p5/run_validation.py', ['--panda', panda, '--opendbc', dbc, '--arm-prefix', python.parent]),
    ('bxcan', 'tests/p5/run_bxcan.py', ['--source', panda, '--opendbc', dbc]),
    ('fdcan', 'tests/p5/run_fdcan.py', ['--source', panda, '--opendbc', dbc]),
    ('bsp', 'tests/f4_bsp/run_bsp.py', ['--source', panda, '--opendbc', dbc, '--arm-prefix', python.parent]),
    ('capabilities', 'tests/p5/run_capabilities.py', ['--panda', panda]),
    ('safety', 'tests/p5/run_safety.py', ['--opendbc', dbc]),
    *[(f'liveness-{t}', 'tests/p5/run_firmware_liveness.py', ['--panda', panda, '--opendbc', dbc, '--target', t]) for t in ('f4', 'h7')],
  ]
  for name, script, flags in jobs:
    destination = out / name
    run(name, [python, panda / script, *flags, '--output', destination])
    report_file = {'protocol': 'validation.json', 'safety': 'safety.json'}.get(name, 'result.json')
    reports[name] = json.loads((destination / report_file).read_text())
    assert reports[name]['status'].startswith('PASS'), name
  pkg = host / 'openpilot/selfdrive/pandad/compatibility'
  flags = ['-std=c++17', '-Wall', '-Wextra', '-Werror', '-O1', '-g', '-fsanitize=address,undefined']
  probe = out / 'health_layout_probe'
  run('compile-host-abi', ['clang++', *flags, '-I', panda / 'board', '-I', dbc, pkg / 'tests/health_layout_probe.cc', '-o', probe])
  layout = out / 'health-layout.json'
  layout.write_text(run('host-abi', [probe]))
  env['TEST_HEALTH_LAYOUT_JSON'] = str(layout)
  run('host-python', [python, pkg / 'tests/run_python_tests.py', out / 'host-python.json'])
  reports['host-python'] = json.loads((out / 'host-python.json').read_text())
  codec = out / 'test_can_codec'
  run('compile-codec', ['clang++', *flags, '-I', pkg, pkg / 'can_codec.cc', pkg / 'tests/test_can_codec.cc', '-o', codec])
  reports['host-codec'] = json.loads(run('codec', [codec]).splitlines()[-1])
  reports['host-cross-language'] = json.loads(run('host-oracle', [python, '-S', pkg / 'tests/check_can_oracle.py', codec, '--health-fixture', layout]))
  deps = json.loads((ROOT / 'patches/openpilot/p2/host-dependencies.json').read_text())['macos']
  for f in deps['files']:
    assert sha(Path(f['path'])) == f['sha256']
  run('host-transport', [python, ROOT / 'tools/run_panda_transport_host.py', '--source', host, '--output', out / 'host-transport',
      '--usb-include', deps['usb_include'], '--usb-library', deps['usb_library']])
  reports['host-transport'] = json.loads((out / 'host-transport/result.json').read_text())
  # Coverage is an acceptance gate, not merely successful process execution.
  assert len(reports['protocol']['abi']) == 2
  assert sum(len(a['negative_compile_cases']) for a in reports['protocol']['abi']) == 6
  assert [a['groups'] for a in reports['protocol']['protocol']] == [15, 15]
  assert reports['bxcan']['groups'] == 24 and reports['fdcan']['groups'] == 14
  assert reports['bsp']['summary']['groups'] == 23
  assert reports['capabilities']['tests_run'] == 12
  assert reports['safety']['tests_run'] == len(set(reports['safety']['selectors'])) == 49
  for target in ('f4', 'h7'):
    assert reports['liveness-' + target]['summary']['passed'] == 13
  assert reports['host-python']['tests_run'] == reports['host-python']['passed'] == 90
  assert reports['host-codec']['passed'] == 13
  assert reports['host-cross-language']['status'] == 'PASS'
  transport = reports['host-transport']['suites']
  counts = {'transport': 19, 'usb_backend': 19, 'spi_backend': 16, 'timing_budget': 4, 'native_link': 1}
  for name, expected in counts.items():
    summary = transport[name]['summary']
    assert summary.get('passed', summary.get('groups', summary.get('groups_passed'))) == expected
    assert not any(summary.get(k) for k in ('failed', 'failures', 'skipped'))
  assert transport['spi_golden']['matched'] == 5
  for name, report in reports.items():
    for key in ('failed', 'failures', 'errors', 'skipped'):
      assert not report.get(key), (name, key)
  # Repeat all source/dependency integrity checks after children have completed.
  assert actual == {str(p.relative_to(panda)): sha(p) for p in panda.rglob('*') if p.is_file() and '.git' not in p.relative_to(panda).parts}
  for path, digest in dbc_inputs.items():
    assert sha(dbc / path) == digest
  for path, digest in hostmanifest['result_files'].items():
    assert sha(host / path) == digest
  for path, digest in {**manifest['baseline_inputs'], **manifest['dependencies']}.items():
    assert sha(ROOT / path) == digest
  # Gate packaging: no device identities/trust and no release key are inferred by compilation.
  result = {'schema': 'songplus-p5-offline-result/v1', 'status': 'PASS_OFFLINE_CANDIDATE',
            'utc': datetime.now(timezone.utc).isoformat(), 'manifest_sha256': sha(PATCH / 'manifest.json'),
            'baselines': manifest['baselines'], 'source_commit': manifest['series'][-1]['commit'],
            'sandbox': 'macos-seatbelt network/device denied', 'reports': reports, 'commands': commands,
            'deployable': False, 'bootstub_built': False, 'device_access': False, 'mcu_executed': False,
            'vehicle_validated': False, 'installation_package_created': False}
  (out / 'result.json').write_text(json.dumps(result, indent=2)+'\n')
  (parent / 'latest.json').write_text(json.dumps({'status': result['status'], 'result': str(out / 'result.json')})+'\n')
  print(json.dumps({'status': result['status'], 'result': str(out / 'result.json')}))


if __name__ == '__main__':
  main()
