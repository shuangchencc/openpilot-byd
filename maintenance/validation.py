"""Fresh P7 offline orchestration and verifiable, nondeployable release evidence."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from maintenance.child_evidence import validate_child_evidence
from maintenance.core import (
  DESIGN,
  HOST,
  LOCK,
  PANDA,
  ROOT,
  capture,
  new_output,
  read_json,
  replay,
  require,
  safe_path,
  sha,
  tree_hashes,
  verify_lock,
  write_json,
)
from maintenance.runtime_guard import (
  BASELINE_SHA,
  DENIED_PERMISSIONS,
  PROTOCOL,
  evaluate_release,
  inspect_runtime,
  synthetic_inventory,
)


def utc():
  return datetime.now(timezone.utc).isoformat()


def json_tail(text):
  decoder = json.JSONDecoder()
  for index, char in enumerate(text):
    if char != '{':
      continue
    try:
      value, end = decoder.raw_decode(text[index:])
      if not text[index + end:].strip():
        return value
    except json.JSONDecodeError:
      pass
  raise ValueError('Child returned no final JSON object')


def child_contract(p5, p6):
  require(p5['status'] == 'PASS_OFFLINE_CANDIDATE' and p6['status'] == 'PASS_OFFLINE_HOST_ONLY', 'Child acceptance failed')
  for data, path in [(p5, PANDA), (p6, HOST)]:
    manifest = read_json(ROOT / path)
    require(data['manifest_sha256'] == sha(ROOT / path) and data['baselines'] == manifest['baselines'], 'Child pin/manifest mismatch')
    require(data['device_access'] is False, 'Child accessed hardware')
    require(all(c['exit_code'] == 0 for c in data['commands']), 'Child command failure')
  require(p5['deployable'] is False and p5['bootstub_built'] is False and p6['deployment_ready'] is False, 'Child scope changed')
  require(p6['patch_commits'] == [p['commit'] for p in read_json(ROOT / HOST)['series']], 'P6 commit mismatch')
  require(p5['source_commit'] == read_json(ROOT / PANDA)['series'][-1]['commit'], 'P5 commit mismatch')
  unit = p6['python_tests']
  require(unit['tests_run'] == unit['passed'] == 94 and not any(unit[k] for k in ['failures', 'errors', 'skipped']), 'P6 Python evidence incomplete')
  require(p6['native_summary']['passed'] == 13 and p6['native_summary']['failed'] == 0, 'Codec evidence incomplete')
  for key in ['native_macos', 'native_linux']:
    require(p6[key]['status'] == 'PASS_OFFLINE_HOST_ONLY', 'Native platform incomplete')
    require(p6[key]['suites']['spi_golden']['matched'] == 5, 'SPI regression incomplete')
  for name in ('f4-debug', 'f4-release', 'h7-debug', 'h7-release'):
    item = p5['reports'][name]
    require(item['build']['deployable'] is False and item['resources']['status'].startswith('PASS'), 'ARM/resource evidence incomplete')
  expected = {c['id'] for c in read_json(ROOT / DESIGN)['cases'] if c['stage'] in ('P0', 'P1', 'P2', 'P3', 'P4', 'P6')}
  require({c['id'] for c in p6['acceptance_results']} == expected and len(p6['acceptance_results']) == len(expected), 'P6 acceptance missing/duplicated')
  require(all(c['status'].startswith('PASS_OFFLINE') for c in p6['acceptance_results']), 'P6 case failure')
  return validate_child_evidence(p5, p6, ROOT)


def acceptance(evidence, registry, run_id, lock_hash):
  p5, p6 = evidence['p5'], evidence['p6']
  child_contract(p5, p6)
  records = []
  def add(case, count, report):
    require(type(count) is int and count > 0, 'Empty executed case: ' + case)
    records.append({'id': case, 'status': 'PASS', 'executed': count, 'failed': 0, 'skipped': 0,
                    'evidence_sha256': registry[report]['sha256'], 'run_id': run_id, 'lock_sha256': lock_hash})
  for case in p6['acceptance_results']:
    count = len(case['python_evidence']) + len(case.get('native_evidence', []))
    for suite, groups in case.get('evidence', {}).items():
      for platform_name in ('macos', 'linux'):
        # A23's frozen P2 map selects the entire native_link suite with [].
        # Count its actually executed groups, never invent a nonzero fallback.
        observed = p6['native_' + platform_name]['suites'][suite]['groups'] if not groups else groups
        require(type(observed) is list and bool(observed) and len(set(observed)) == len(observed),
                'Missing executed suite groups: ' + case['id'] + '/' + platform_name + '/' + suite)
        count += len(observed)
    count += 2 * len((case.get('safety_evidence') or {}).get('required_cases', []))
    count += int(case.get('independent_oracle', False))
    add(case['id'], count, 'p6')
  # Counts are executed evidence groups/checks, NOT an additive unique test total.
  for case, count in [('A50', 2), ('A51', 4), ('A52', 38), ('A53', 4), ('A54', 129), ('A55', 4)]:
    add(case, count, 'p5')
  contract = evidence['contract']
  require(contract['status'] == 'PASS' and contract['failed'] == contract['skipped'] == 0, 'CI contract not passed')
  expected_commands = {'metadata-lock', 'help-root', 'help-verify-lock', 'help-replay', 'help-validate',
                       'help-release-check', 'help-inspect-runtime', 'maintenance-tests', 'ci-tests'}
  require(len(contract['commands']) == len(expected_commands)
          and {c['name'] for c in contract['commands']} == expected_commands
          and contract['passed_commands'] == len(expected_commands)
          and all(c['returncode'] == c['expected_returncode'] == 0 for c in contract['commands']), 'CI commands incomplete')
  suite_counts = {c['name']: c.get('tests_passed', 0) for c in contract['commands']}
  require(suite_counts['maintenance-tests'] >= 69 and suite_counts['ci-tests'] == 21, 'Maintenance/CI tests incomplete')
  require(evidence['repro']['status'] == 'PASS' and evidence['repro']['candidate_builds_compared'] == 8, 'Two-build reproducibility incomplete')
  replay_evidence = evidence['source_replay']
  require(replay_evidence['status'] == 'PASS' and replay_evidence['bundles_equal'] is True and replay_evidence['trees_equal'] is True, 'Source replay mismatch')
  gates = evidence['gates']
  require(gates['status'] == 'PASS' and gates['positive']['inspection_valid'] is True
          and gates['positive']['offline_artifacts_available'] is True
          and gates['positive']['runtime_available'] is False, 'Artifact gate failed')
  require(gates['negative_count'] == len(gates['negative']) == 5
          and all(r['inspection_valid'] is False and r['runtime_available'] is False and r['reasons'] for r in gates['negative']), 'Runtime negative gate missing')
  variants = {'f4-debug', 'f4-release', 'h7-debug', 'h7-release'}
  builds = evidence['second_builds']
  require(builds['status'] == 'PASS' and builds['builds'] == 4 and set(builds['reports']) == variants, 'Second build set incomplete')
  names = {prefix + name for name in variants for prefix in ('rebuild-', 'resources-')}
  require(len(builds['commands']) == 8 and {c['name'] for c in builds['commands']} == names
          and all(c['exit_code'] == 0 for c in builds['commands']), 'Second compilation incomplete')
  comparisons = evidence['repro']['results']
  require(evidence['repro']['build_variants'] == 4 and set(comparisons) == variants, 'Reproducibility comparison set incomplete')
  expected_artifacts = []
  for name, result in sorted(comparisons.items()):
    require(result['core_candidate_bytes_equal'] is True
            and result['byte_equal']['main.bin'] is True and result['byte_equal']['main.bin.signed'] is True, 'Candidate bytes differ')
    left, right = result['first'], result['second']
    for field in ('source_identity', 'version', 'compiler', 'protocol_inputs', 'elf'):
      require(left[field] == right[field], 'Reproducibility metadata mismatch')
    require(left['signature_verified'] is True and right['signature_verified'] is True, 'Signature verification missing')
    for field in ('source_root', 'opendbc_root', 'output_root'):
      require(left['provenance'][field] != right['provenance'][field], 'Source/output directory reused')
    build = builds['reports'][name]
    require(build['resources']['status'].startswith('PASS') and build['build']['source_identity'] == right['source_identity'], 'Second resource/source evidence mismatch')
    for filename, artifact in right['artifacts'].items():
      require(build['build']['artifacts'].get(filename) == artifact['sha256'], 'Second artifact metadata mismatch')
    for filename in ('main.bin', 'main.bin.signed'):
      require(left['artifacts'][filename] == right['artifacts'][filename]
              and p5['reports'][name]['build']['artifacts'][filename] == left['artifacts'][filename]['sha256'], 'First/second artifact mismatch')
    for filename, kind in [('main.elf', 'firmware-elf'), ('main.bin', 'firmware-bin'), ('main.bin.signed', 'firmware-signed-bin')]:
      artifact = right['artifacts'][filename]
      target, policy = name.split('-')
      expected_artifacts.append({'path': name + '/' + filename, 'sha256': artifact['sha256'],
          'size_bytes': artifact['bytes'], 'kind': kind, 'mcu': target.upper(), 'build_variant': policy,
          'protocol': PROTOCOL, 'signature': 'public-development-key' if filename.endswith('.signed') else 'unsigned',
          'deployable': False})
  require(evidence['artifacts']['artifacts'] == expected_artifacts, 'Candidate bundle is not the compiled artifact set')
  add('A70', gates['negative_count'] + 1, 'gates')
  add('A71', 8, 'repro')
  add('A72', 41, 'contract')  # exhaustive missing-case release tests in contract suite
  return sorted(records, key=lambda c: c['id'])


def check_command_logs(report, base):
  expected = {'ci-contract', 'p5', 'p6', 'reproducibility'}
  expected.update(prefix + name for prefix in ('rebuild-', 'resources-')
                  for name in ('f4-debug', 'f4-release', 'h7-debug', 'h7-release'))
  commands = report['commands']
  require(len(commands) == len(expected) and {c['name'] for c in commands} == expected, 'P7 command log set incomplete')
  for command in commands:
    require(command['exit_code'] == 0 and command['argv'], 'P7 command did not execute successfully')
    require(sha(safe_path(base, command['name'] + '.log')) == command['log_sha256'], 'P7 command log drift')


def release_check(report_path):
  verify_lock(metadata_only=True)
  report_path = Path(report_path).resolve()
  report = read_json(report_path)
  require(report['schema'] == 'songplus-p7-offline-validation/v1' and report['status'] == 'PASS_OFFLINE_COMPATIBILITY', 'Not a complete P7 report')
  require(report['lock_sha256'] == sha(ROOT / LOCK), 'Report is for another maintenance lock')
  require(report['device_access'] is False and report['deployment_ready'] is False, 'Invalid report scope')
  check_command_logs(report, report_path.parent)
  expected_names = {'p5', 'p6', 'contract', 'repro', 'source_replay', 'gates', 'second_builds', 'artifacts'}
  require(set(report['evidence']) == expected_names, 'Evidence registry incomplete')
  evidence = {}
  for name, item in report['evidence'].items():
    require(item['run_id'] == report['run_id'] and item['lock_sha256'] == report['lock_sha256'], 'Stale evidence context')
    path = safe_path(report_path.parent, item['path'])
    require(sha(path) == item['sha256'], 'Evidence hash mismatch: ' + name)
    evidence[name] = read_json(path)
  require(evidence['source_replay']['lock_sha256'] == report['lock_sha256'], 'Replay lock mismatch')
  start, end = (datetime.fromisoformat(report[k]) for k in ('started_at_utc', 'completed_at_utc'))
  require(start <= end, 'Invalid execution interval')
  for name, field in [('p5', 'utc'), ('p6', 'tested_at_utc'), ('contract', 'completed_at_utc')]:
    require(start <= datetime.fromisoformat(evidence[name][field]) <= end, 'Historical evidence cannot count as a fresh run: ' + name)
  cases = acceptance(evidence, report['evidence'], report['run_id'], report['lock_sha256'])
  require(report['acceptance'] == cases, 'Case records differ from executed evidence')
  gate = evaluate_release(cases, evidence_context={'run_id': report['run_id'], 'lock_sha256': report['lock_sha256']})
  require(gate['offline_compatibility_complete'], 'Offline acceptance incomplete')
  gate['deployment_blockers'] = gate['blockers']
  # Independently verify retained real artifact bytes when checking a local build bundle.
  inspected = inspect_runtime(synthetic_inventory(), evidence['artifacts'], safe_path(report_path.parent, 'artifacts'))
  require(inspected['inspection_valid'] and inspected['offline_artifacts_available'], 'Candidate artifact bundle missing/drifted')
  require(inspected == evidence['gates']['positive'], 'Artifact gate changed after validation')
  require(report['release'] == gate, 'Stored release conclusion differs from gate')
  return gate


def validate(output, linux_instance, linux_root):
  require(platform.system() == 'Darwin', 'Full entry requires the declared preprovisioned macOS + local Lima environment')
  require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', linux_instance) is not None, 'Invalid local Lima instance name')
  require(linux_root.startswith('/home/') and '..' not in Path(linux_root).parts, 'Invalid prepared Linux root')
  require(Path(os.path.abspath(output)).is_relative_to(ROOT / 'results'), 'Validation output must be below repository results/')
  verified = verify_lock()
  protected = [LOCK, *read_json(ROOT / LOCK)['files']]
  capture(['git', 'ls-files', '--error-unmatch', *protected])
  capture(['git', 'diff', '--exit-code', 'HEAD', '--', *protected])
  maintenance_commit = capture(['git', 'rev-parse', 'HEAD']).decode().strip()
  out = new_output(output)
  run_id, started = 'p7-' + uuid.uuid4().hex, utc()
  wall_start = time.time()
  lock_hash = verified['lock_sha256']
  python = ROOT / 'upstream-openpilot/.venv/bin/python'
  sandbox = ['/usr/bin/sandbox-exec', '-f', str(ROOT / 'tools/panda_firmware_offline.sb')]
  env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', PYTHONOPTIMIZE='0',
             UV_OFFLINE='1', PIP_NO_INDEX='1', P2_NETWORK_DEVICE_SANDBOX='macos-seatbelt')
  commands = []
  write_json(out / 'result.json', {'status': 'RUNNING', 'run_id': run_id, 'device_access': False})
  def run(name, argv, isolated=True):
    full = [*sandbox, *map(str, argv)] if isolated else list(map(str, argv))
    result = subprocess.run(full, cwd=ROOT, env=env, capture_output=True, timeout=3000, check=False)
    log = out / (name + '.log')
    log.write_bytes(result.stdout + result.stderr)
    item = {'name': name, 'argv': full, 'exit_code': result.returncode, 'log_sha256': sha(log)}
    commands.append(item)
    require(result.returncode == 0, f'{name}: exit {result.returncode}; see {log}')
    return result.stdout.decode()
  try:
    first = replay(out / 'replay')
    second = replay(out / 'replay-check')
    require(first['bundle_sha256'] == second['bundle_sha256'], 'Deterministic source bundle mismatch')
    snapshot = first['files']
    evidence = {'source_replay': {'status': 'PASS', 'lock_sha256': lock_hash,
                 'bundles_equal': True, 'bundle_sha256': first['bundle_sha256'],
                 'trees_equal': first['files'] == second['files'],
                 'source_counts': {k: len(v) for k, v in snapshot.items()},
                 'commands': first['commands'] + second['commands']}}
    run('ci-contract', [python, '-B', ROOT / 'tools/check_panda_ci.py', '--mode', 'contract', '--output', out / 'contract'])
    evidence['contract'] = read_json(out / 'contract/result.json')
    def old_validator(name, tool, flags):
      text = run(name, [python, ROOT / tool, *flags], isolated=False)
      tail = json_tail(text)
      result = Path(tail['result']).resolve()
      allowed = ROOT / ('results/panda-firmware-p5' if name == 'p5' else 'results/panda-compat-p6')
      require(result.is_relative_to(allowed) and result.stat().st_mtime >= wall_start, 'Stale/unexpected child report')
      return result, read_json(result)
    # Only orchestration/Lima IPC is outside Seatbelt. Frozen workers sandbox every test/compiler.
    with ThreadPoolExecutor(max_workers=2) as pool:
      p5_future = pool.submit(old_validator, 'p5', 'tools/validate_panda_firmware.py', [])
      p6_future = pool.submit(old_validator, 'p6', 'tools/validate_panda_p6.py',
                              ['--linux-instance', linux_instance, '--linux-root', linux_root])
      p5_path, evidence['p5'] = p5_future.result()
      panda, dbc = out / 'replay/panda', out / 'replay/opendbc'
      rebuild = out / 'rebuild'
      rebuild.mkdir()
      build_start = len(commands)
      for target in ('f4', 'h7'):
        for policy in ('debug', 'release'):
          name = target + '-' + policy
          directory = rebuild / name
          run('rebuild-' + name, [python, panda / 'tools/p5/build_candidate.py', '--source', panda, '--opendbc', dbc,
              '--arm-prefix', python.parent, '--output', directory, '--target', target, '--policy', policy])
          run('resources-' + name, [python, panda / 'tests/p5/audit_resources.py', '--target', target,
              '--elf', directory / 'main.elf', '--map', directory / 'main.map', '--raw', directory / 'main.bin',
              '--signed', directory / 'main.bin.signed', '--public-key', panda / 'board/crypto/certs/debug.pub',
              '--stack-dir', directory, '--output', directory / 'resources.json'])
      evidence['second_builds'] = {'status': 'PASS', 'builds': 4,
          'commands': [c for c in commands[build_start:] if c['name'].startswith(('rebuild-', 'resources-'))],
          'reports': {name: {'build': read_json(rebuild / name / 'build.json'),
                            'resources': read_json(rebuild / name / 'resources.json')}
                      for name in ('f4-debug', 'f4-release', 'h7-debug', 'h7-release')}}
      run('reproducibility', [python, ROOT / 'maintenance/reproducibility.py', '--first', p5_path.parent,
           '--second', rebuild, '--public-key', panda / 'board/crypto/certs/debug.pub', '--output', out / 'repro.json'])
      evidence['repro'] = read_json(out / 'repro.json')
      p6_path, evidence['p6'] = p6_future.result()
    verified_counts = child_contract(evidence['p5'], evidence['p6'])
    artifact_root = out / 'artifacts'
    artifact_root.mkdir()
    artifacts = {'schema': 'songplus-maintenance-artifacts/v1', 'baseline_sha': BASELINE_SHA,
                 'deployable': False, 'permissions': dict(DENIED_PERMISSIONS), 'artifacts': []}
    for name in ('f4-debug', 'f4-release', 'h7-debug', 'h7-release'):
      target, policy = name.split('-')
      for file, kind in [('main.elf', 'firmware-elf'), ('main.bin', 'firmware-bin'), ('main.bin.signed', 'firmware-signed-bin')]:
        destination = artifact_root / name / file
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(rebuild / name / file, destination)
        artifacts['artifacts'].append({'path': str(destination.relative_to(artifact_root)), 'sha256': sha(destination),
            'size_bytes': destination.stat().st_size, 'kind': kind, 'mcu': target.upper(), 'build_variant': policy,
            'protocol': PROTOCOL, 'signature': 'public-development-key' if file.endswith('.signed') else 'unsigned',
            'deployable': False})
    evidence['artifacts'] = artifacts
    positive = inspect_runtime(synthetic_inventory(), artifacts, artifact_root)
    negatives = []
    for field in ('board', 'protocol', 'signature'):
      inventory = synthetic_inventory()
      inventory['slots'][0][field] = 'unknown'
      negatives.append(inspect_runtime(inventory, artifacts, artifact_root))
    missing = json.loads(json.dumps(artifacts))
    missing['artifacts'][0]['path'] = 'missing.bin'
    negatives.append(inspect_runtime(synthetic_inventory(), missing, artifact_root))
    corrupt = json.loads(json.dumps(artifacts))
    corrupt['artifacts'][0]['sha256'] = '0' * 64
    negatives.append(inspect_runtime(synthetic_inventory(), corrupt, artifact_root))
    evidence['gates'] = {'status': 'PASS', 'positive': positive, 'negative': negatives, 'negative_count': len(negatives),
                         'side_effect_spies_and_missing_case_matrix': 'contract maintenance/test_runtime_guard.py',
                         'runtime_available': False, 'deployment_ready': False}
    require(all(tree_hashes(out / 'replay' / name) == values for name, values in snapshot.items()), 'Build mutated replay source')
    verify_lock()
    registry = {}
    for name, value in evidence.items():
      path = out / 'evidence' / (name + '.json')
      write_json(path, value)
      registry[name] = {'path': str(path.relative_to(out)), 'sha256': sha(path), 'run_id': run_id, 'lock_sha256': lock_hash}
    cases = acceptance(evidence, registry, run_id, lock_hash)
    gate = evaluate_release(cases, evidence_context={'run_id': run_id, 'lock_sha256': lock_hash})
    gate['deployment_blockers'] = gate['blockers']
    require(gate['offline_compatibility_complete'], 'Required offline acceptance incomplete')
    result = {'schema': 'songplus-p7-offline-validation/v1', 'status': 'PASS_OFFLINE_COMPATIBILITY',
        'run_id': run_id, 'started_at_utc': started, 'completed_at_utc': utc(), 'lock_sha256': lock_hash,
        'baselines': read_json(ROOT / HOST)['baselines'], 'preflight': verified,
        'maintenance_commit': maintenance_commit, 'protected_maintenance_files_match_commit': True,
        'fresh_child_reports': {'p5': str(p5_path), 'p6': str(p6_path)},
        'evidence': registry, 'acceptance': cases, 'release': gate, 'commands': commands,
        'verified_child_counts': verified_counts,
        'counts_note': 'Acceptance executed counts are overlapping evidence groups/checks, not additive test counts.',
        'device_access': False, 'deployment_ready': False, 'remote_ci_executed': False,
        'actual_mcu_executed': False, 'vehicle_validated': False}
    write_json(out / 'result.json', result)
    release_check(out / 'result.json')
    write_json(ROOT / 'results/panda-compat-p7/latest.json', {'status': result['status'], 'result': str(out / 'result.json')})
    return result
  except Exception as error:
    write_json(out / 'result.json', {'status': 'FAIL', 'error': str(error), 'run_id': run_id,
                'started_at_utc': started, 'completed_at_utc': utc(), 'commands': commands,
                'device_access': False, 'deployment_ready': False})
    raise
