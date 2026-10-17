#!/usr/bin/env python3
"""Build the real Linux pandad and run its explicit offline dispatch only.

Dependencies must already exist in --prefix. No downloader, installer, device
enumeration, Panda construction or legacy daemon invocation is performed by
this worker. The caller must establish its network/device-denying sandbox.
macOS builds the actual cereal/runtime fixture, since upstream spi.cc requires
Linux spidev; it is never represented as a full daemon build.
"""
import argparse
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

COMPAT_SOURCES = [
  'session_registry.cc', 'can_router.cc', 'can_codec.cc', 'transport.cc',
  'control_session.cc', 'pandad_runtime.cc', 'cereal_bridge.cc', 'offline_entry.cc',
  'pandad_loop.cc',
]
DAEMON_SOURCES = [
  'openpilot/selfdrive/pandad/main.cc',
  'openpilot/selfdrive/pandad/pandad.cc',
  'openpilot/selfdrive/pandad/panda_safety.cc',
  'openpilot/selfdrive/pandad/panda.cc',
  'openpilot/selfdrive/pandad/spi.cc',
  'openpilot/common/params.cc',
  'openpilot/common/swaglog.cc',
  'openpilot/common/util.cc',
  'openpilot/common/ratekeeper.cc',
  'openpilot/cereal/messaging/socketmaster.cc',
  'msgq_repo/msgq/ipc.cc',
  'msgq_repo/msgq/event.cc',
  'msgq_repo/msgq/impl_msgq.cc',
  'msgq_repo/msgq/impl_fake.cc',
  'msgq_repo/msgq/msgq.cc',
]


def sha256(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--source', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  parser.add_argument('--prefix', type=Path, required=True,
                      help='Prepared purelib layout containing capnproto/json11/zeromq install directories')
  parser.add_argument('--compiler', default='g++')
  parser.add_argument('--entry-test', type=Path,
                      help='macOS-only fixture main source; never substitutes any Linux daemon source')
  parser.add_argument('--unknown-exit-code', type=int, default=2)
  parser.add_argument('--jobs', type=int, default=2)
  args = parser.parse_args()
  if not 1 <= args.jobs <= 4:
    raise ValueError('jobs must be in 1..4')
  system = platform.system()
  if system not in ('Linux', 'Darwin'):
    raise ValueError('Only Linux daemon / macOS cereal fixture are supported')
  args.source = args.source.resolve()
  args.output = args.output.resolve()
  args.prefix = args.prefix.resolve()
  args.output.mkdir(parents=True, exist_ok=True)
  sandbox = os.environ.get('P2_NETWORK_DEVICE_SANDBOX')
  if not sandbox:
    raise ValueError('Caller must establish the existing offline network/device sandbox')
  if system == 'Darwin' and args.entry_test is None:
    raise ValueError('macOS requires an explicit --entry-test fixture; no SPI substitution is provided')
  generated = args.output / 'generated'
  cpp = generated / 'openpilot/cereal/gen/cpp'
  cpp.mkdir(parents=True, exist_ok=True)
  capn = args.prefix / 'capnproto/install'
  packages = ['capnproto'] + (['json11', 'zeromq'] if system == 'Linux' else [])
  libs = [capn / 'lib/libcapnp.a', capn / 'lib/libkj.a']
  if system == 'Linux':
    libs += [args.prefix / 'json11/install/lib/libjson11.a', args.prefix / 'zeromq/install/lib/libzmq.a']
  capnpc = capn / 'bin/capnpc'
  for path in [capnpc, capn / 'bin/capnpc-c++', *libs]:
    if not path.is_file():
      raise FileNotFoundError('Missing prepared dependency: ' + str(path))
  env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1',
             UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1',
             PWD=str(args.source),
             PATH=str(capn / 'bin') + os.pathsep + os.environ.get('PATH', ''))
  commands = []
  source_hashes = {}

  def run(name, argv, timeout=120, expected=0):
    argv = list(map(str, argv))
    result = subprocess.run(argv, cwd=args.source, capture_output=True, env=env, timeout=timeout, check=False)
    (args.output / (name + '.stdout')).write_bytes(result.stdout)
    (args.output / (name + '.stderr')).write_bytes(result.stderr)
    commands.append({'name': name, 'command': argv, 'exit_code': result.returncode,
                     'expected_exit_code': expected})
    if result.returncode != expected:
      raise RuntimeError(name + ' exited ' + str(result.returncode) + '; see ' + str(args.output / (name + '.stderr')))
    return result.stdout.decode()

  try:
    cereal = args.source / 'openpilot/cereal'
    car = args.source / 'opendbc_repo/opendbc/car'
    schemas = [cereal / name for name in ('log.capnp', 'deprecated.capnp', 'custom.capnp')] + [car / 'car.capnp']
    for path in [*schemas, cereal / 'include/c++.capnp']:
      source_hashes[str(path.relative_to(args.source))] = sha256(path)
    run('generate-cereal', [capnpc, '--src-prefix=' + str(cereal), '--src-prefix=' + str(car),
         '--import-path=' + str(car), '--import-path=' + str(cereal),
         '-o', 'c++:' + str(cpp), *schemas])
    service_source = cereal / 'services.py'
    service_text = run('generate-services', [sys.executable, '-S', service_source])
    (generated / 'openpilot/cereal/services.h').write_text(service_text)
    source_hashes[str(service_source.relative_to(args.source))] = sha256(service_source)
    pkg = args.source / 'openpilot/selfdrive/pandad/compatibility'
    files = [(pkg / name, True) for name in COMPAT_SOURCES]
    files += [(cpp / (name + '.capnp.c++'), False) for name in ('log', 'deprecated', 'custom', 'car')]
    if system == 'Linux':
      files += [(args.source / name, False) for name in DAEMON_SOURCES]
    else:
      files.append((args.entry_test.resolve(), True))
    flags = ['-std=c++17', '-Wall', '-Wextra', '-O1', '-g', '-fno-omit-frame-pointer',
             '-fsanitize=address,undefined', '-pthread', '-D__COMMA_HARDWARE__=0',
             '-I', generated, '-I', generated / 'openpilot', '-I', cpp,
             '-I', args.source, '-I', args.source / 'openpilot', '-I', pkg,
             '-I', args.source / 'msgq_repo', '-I', args.source / 'opendbc_repo']
    for package in packages:
      flags += ['-isystem', args.prefix / package / 'install/include']
    if system == 'Linux':
      # The pinned msgq SConscript supplies this exact logging include macro.
      flags += ['-DSWAGLOG="common/swaglog.h"']

    def compile_one(item):
      i, (source, strict) = item
      name = f'compile-{i:02d}-' + source.name.replace('.', '-')
      obj = args.output / f'{i:02d}.o'
      dep = args.output / f'{i:02d}.d'
      run(name, [args.compiler, *flags, *(['-Werror'] if strict else []),
                 '-MMD', '-MF', dep, '-c', source, '-o', obj], timeout=180)
      return obj, dep, source

    # Every independent compilation is awaited; no exception is discarded.
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
      built = list(pool.map(compile_one, enumerate(files)))
    loop_built = compile_one((len(files), (pkg / 'tests/test_pandad_loop.cc', True)))
    for _, dep, source in [*built, loop_built]:
      for dependency in [source, *map(Path, shlex.split(dep.read_text().replace('\\\n', ' ').split(':', 1)[1]))]:
        if not dependency.is_absolute():
          dependency = args.source / dependency
        dependency = dependency.resolve()
        if dependency.is_relative_to(args.source):
          source_hashes[str(dependency.relative_to(args.source))] = sha256(dependency)
    binary = args.output / ('pandad' if system == 'Linux' else 'cereal_runtime_fixture')
    run('link', [args.compiler, '-fsanitize=address,undefined', '-pthread',
                *[obj for obj, _, _ in built], *libs, '-lz',
                *(['-ldl', '-lrt'] if system == 'Linux' else []), '-o', binary], timeout=180)
    # Never run the Linux daemon with no arguments: that is the retained
    # real-hardware path. The distinct macOS fixture only calls the offline main.
    raw = run('offline-entry', [binary, *(['--compat-offline'] if system == 'Linux' else [])], timeout=90)
    lines = raw.splitlines()

    def parse_suite(raw, label):
      transcript = raw.splitlines()
      summary = json.loads(transcript[-1])
      if summary.get('failed', summary.get('failures', 0)) or summary.get('skipped', summary.get('skips', 0)):
        raise ValueError(label + ' reported a failed/skipped check')
      groups = [line[5:] for line in transcript if line.startswith('PASS ')]
      if not groups or len(groups) != len(set(groups)):
        raise ValueError(label + ' must report unique passing groups')
      count = summary.get('passed', summary.get('groups_passed', summary.get('groups')))
      if count != len(groups):
        raise ValueError(label + ' summary count differs from passing transcript')
      if 'cases' in summary and summary['cases'] != groups:
        raise ValueError(label + ' summary case list differs from passing transcript')
      return {'summary': summary, 'groups': groups,
              'traces': [line[6:] for line in transcript if line.startswith('TRACE ')]}

    entry_suite = parse_suite(raw, 'Offline entry')
    loop_binary = args.output / 'test_pandad_loop'
    # This second fixture uses the very same compiled core and real cereal
    # objects, with its own main. No legacy daemon symbols are substituted.
    loop_objects = [obj for obj, _, source in built
                    if ((source.parent == pkg and source.name != 'offline_entry.cc') or source.parent == cpp)]
    run('link-loop-fixture', [args.compiler, '-fsanitize=address,undefined', '-pthread',
                             *loop_objects, loop_built[0], *libs[:2], '-o', loop_binary], timeout=180)
    loop_suite = parse_suite(run('loop-fixture', [loop_binary], timeout=90), 'Pandad loop fixture')
    loop_suite['binary_sha256'] = sha256(loop_binary)
    unknown = None
    guard_cases = []
    if system == 'Linux':
      for name, arguments in (
          ('unknown-compat-guard', ['--compat-unknown']),
          ('offline-extra-guard', ['--compat-offline', 'extra']),
          ('serial-offline-guard', ['fixture:serial', '--compat-offline']),
          ('serial-unknown-guard', ['fixture:serial', '--compat-unknown']),
          ('third-argument-guard', ['fixture:serial', 'extra', '--compat-unknown']),
          ('multiple-compat-guard', ['--compat-offline', '--compat-unknown']),
          ('empty-serial-guard', ['', '--compat-offline']),
          ('compat-value-guard', ['fixture:serial', '--compat-offline=1']),
      ):
        stdout = run(name, [binary, *arguments], timeout=10, expected=args.unknown_exit_code)
        guard_cases.append({'name': name, 'arguments': arguments,
                            'exit_code': args.unknown_exit_code, 'stdout': stdout})
        if name == 'unknown-compat-guard':
          unknown = stdout
    report = {'status': 'PASS_OFFLINE_HOST_ONLY', 'platform': platform.platform(),
              'scope': 'actual Linux pandad executable' if system == 'Linux' else 'macOS native cereal/runtime fixture',
              'active_pandad_built': system == 'Linux',
              'actual_main_cc_compiled_and_linked': system == 'Linux',
              'legacy_panda_spi_compiled_and_linked': system == 'Linux',
              'legacy_main_invoked': False, 'hardware_open': False, 'real_can_tx': False,
              'sandbox': sandbox, 'compiler_version': run('compiler-version', [args.compiler, '--version']),
              'cereal_compiler_version': run('capnp-version', [capn / 'bin/capnp', '--version']),
              'source_files_sha256': source_hashes,
              'dependency_files_sha256': {str(p.relative_to(args.prefix)): sha256(p) for p in [capnpc, capn / 'bin/capnpc-c++', *libs]},
              'generated_files_sha256': {str(p.relative_to(generated)): sha256(p) for p in generated.rglob('*') if p.is_file()},
              'binary_sha256': sha256(binary), 'commands': commands,
              **entry_suite, 'loop_suite': loop_suite,
              'unknown_compat_guard': {'exit_code': args.unknown_exit_code, 'stdout': unknown,
                                       'cases': guard_cases} if system == 'Linux' else None,
              'sanitizers': ['address', 'undefined'],
              'upstream_warning_policy': 'Compiler warnings retained; -Werror applied to new compatibility sources only'}
    (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    for line in lines:
      print(line)
  except Exception as exc:
    (args.output / 'result.json').write_text(json.dumps({'status': 'FAIL', 'error': str(exc),
                                                       'commands': commands}, indent=2) + '\n')
    raise


if __name__ == '__main__':
  main()
