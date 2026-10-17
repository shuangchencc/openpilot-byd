#!/usr/bin/env python3
"""Local native P0/P1/P2/P3/P4 build/test worker; requires a caller-supplied offline sandbox.

Never resolves dependencies or runs hardware enumeration/opening. Linux Python
is only the build runner; P0/P1 protocol Python tests use the locked host Python.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def main():
  p = argparse.ArgumentParser(description=__doc__)
  p.add_argument('--source', type=Path, required=True)
  p.add_argument('--output', type=Path, required=True)
  p.add_argument('--usb-include', type=Path, required=True)
  p.add_argument('--usb-library', type=Path, required=True)
  p.add_argument('--compiler', default='clang++')
  a = p.parse_args()
  if not os.environ.get("P2_NETWORK_DEVICE_SANDBOX"):
    raise ValueError("Caller must establish the offline network/device sandbox before building")
  a.output.mkdir(parents=True, exist_ok=True)
  pkg = a.source / 'openpilot/selfdrive/pandad/compatibility'
  env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1',
             UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
  commands = []
  def run(name, argv):
    argv = list(map(str, argv))
    r = subprocess.run(argv, capture_output=True, env=env, timeout=90)
    (a.output / (name + '.stdout')).write_bytes(r.stdout)
    (a.output / (name + '.stderr')).write_bytes(r.stderr)
    commands.append({'name': name, 'command': argv, 'exit_code': r.returncode})
    r.check_returncode()
    return r.stdout.decode()
  flags = ['-std=c++17', '-Wall', '-Wextra', '-Werror', '-O1', '-g', '-fno-omit-frame-pointer', '-fsanitize=address,undefined', '-pthread',
           '-I', pkg, '-I', a.source / 'openpilot', '-I', a.source]
  specs = {
    'transport': ['transport.cc'],
    'usb_backend': ['transport.cc', 'usb_backend.cc', 'libusb_api.cc', 'can_codec.cc'],
    'spi_backend': ['transport.cc', 'spi_backend.cc'],
    'timing_budget': [],
    'native_link': ['transport.cc', 'usb_backend.cc', 'libusb_api.cc', 'spi_backend.cc', 'session_registry.cc', 'can_router.cc', 'can_codec.cc', 'control_session.cc'],
    'session_registry': ['session_registry.cc'],
    'can_router': ['session_registry.cc', 'can_router.cc', 'can_codec.cc'],
    'control_session': ['session_registry.cc', 'control_session.cc'],
    'session_router_integration': ['session_registry.cc', 'can_router.cc', 'can_codec.cc', 'transport.cc'],
  }
  if platform.system() == 'Linux': specs['native_link'].append('linux_spi_io.cc')
  results = {}
  for name, sources in specs.items():
    binary = a.output / ('test_' + name)
    link = []
    if name in ('usb_backend', 'native_link'):
      link = ['-I', a.usb_include, a.usb_library, '-Wl,-rpath,' + str(a.usb_library.parent)]
    run('compile-' + name, [a.compiler, *flags, *[pkg / s for s in sources],
                           pkg / f'tests/test_{name}.cc', *link, '-o', binary])
    raw = run('test-' + name, [binary])
    groups = [line[5:] for line in raw.splitlines() if line.startswith('PASS ')]
    summary = json.loads(raw.splitlines()[-1])
    if summary.get('failed', summary.get('failures', 0)) or summary.get('skipped', 0) or summary.get('skips', 0):
      raise ValueError('Failed/skipped suite: ' + name)
    count = summary.get('passed', summary.get('groups_passed', summary.get('groups')))
    effective_groups = groups or summary.get('cases', [])
    # The unchanged P2 native-link smoke emits one summary rather than PASS
    # lines; its single checked contract is named explicitly by this worker.
    if name == 'native_link' and not effective_groups:
      if summary.get('suite') != 'native_link' or summary.get('device_open') is not False:
        raise ValueError('Invalid native-link evidence')
      effective_groups = ['native_link_inert_construction']
    if not count or count != len(effective_groups) or len(set(effective_groups)) != count:
      raise ValueError('Invalid suite counts')
    if groups and 'cases' in summary and summary['cases'] != groups:
      raise ValueError('Case list differs from passing transcript')
    results[name] = {'summary': summary, 'groups': effective_groups,
                     'traces': [line[6:] for line in raw.splitlines() if line.startswith('TRACE ')]}
  reference = a.output / 'spi_reference_probe'
  run('compile-spi-reference', [a.compiler, *flags, pkg / 'tests/spi_reference_probe.cc', '-lz', '-o', reference])
  old = run('spi-reference-golden', [reference])
  new = run('spi-candidate-golden', [a.output / 'test_spi_backend', '--golden'])
  if old != new or len(old.splitlines()) != 5: raise ValueError('SPI golden mismatch')
  results['spi_golden'] = {'matched': 5, 'vectors': old.splitlines(), 'original_handle_constructed': False}
  p3_oracle = json.loads(run('session-oracle-vectors', [a.output / 'test_session_router_integration', '--oracle']))
  (a.output / 'session-oracle.json').write_text(json.dumps(p3_oracle, indent=2) + '\n')
  firmware_dir = a.output / 'firmware-liveness'
  run('firmware-liveness', [sys.executable, '-S', pkg / 'tests/run_firmware_liveness.py',
      '--source', a.source, '--output', firmware_dir, '--compiler', a.compiler])
  firmware = json.loads((firmware_dir / 'result.json').read_text())
  firmware_summary = firmware['summary']
  firmware_groups = firmware_summary['cases']
  if (firmware['status'] != 'PASS_OFFLINE_SOURCE_ORACLE' or
      firmware_summary['passed'] != len(firmware_groups) or len(set(firmware_groups)) < 13 or
      firmware_summary['failed'] or firmware_summary['skipped'] or
      not firmware_summary['actual_pinned_c_executed'] or firmware_summary['hardware_tested']):
    raise ValueError('Invalid pinned firmware oracle evidence')
  results['firmware_liveness'] = {'summary': firmware_summary, 'groups': firmware_groups,
                                  'traces': firmware['traces']}
  report = {'firmware_liveness_oracle': firmware, 'session_oracle_vectors': p3_oracle, 'status': 'PASS_OFFLINE_HOST_ONLY', 'platform': platform.platform(),
            'compiler_version': run('compiler-version', [a.compiler, '--version']),
            'sandbox': os.environ.get('P2_NETWORK_DEVICE_SANDBOX'), 'suites': results,
            'commands': commands, 'sanitizers': ['address', 'undefined'],
            'real_libusb_linked': True, 'real_linux_spi_linked': platform.system() == 'Linux',
            'active_pandad_built': False, 'device_access': False, 'hardware_timing_validated': False}
  if not report['sandbox']: raise ValueError('Caller did not establish offline sandbox')
  (a.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
  print(json.dumps({'status': report['status'], 'platform': report['platform'],
                    'groups': {k: v['summary'] for k, v in results.items() if 'summary' in v}}))

if __name__ == '__main__':
  main()
