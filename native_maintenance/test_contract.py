from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from maintenance import core as old
from native_maintenance import core, evidence
from tools import build_p9_native as worker
from tools import check_songplus_native_ci as ci


class SuiteEvidenceTests(unittest.TestCase):
  def valid(self):
    return 'PASS a\nPASS b\n' + json.dumps({'passed': 2, 'failed': 0, 'skipped': 0})

  def test_valid_transcript(self):
    self.assertEqual(worker.parse_suite(self.valid(), 'unit')['groups'], ['a', 'b'])

  def test_empty_transcript_rejected(self):
    with self.assertRaises(ValueError):
      worker.parse_suite('', 'unit')

  def test_duplicate_group_rejected(self):
    with self.assertRaises(ValueError):
      worker.parse_suite(self.valid().replace('PASS b', 'PASS a'), 'unit')

  def test_missing_group_rejected(self):
    with self.assertRaises(ValueError):
      worker.parse_suite(self.valid().replace('PASS b\n', ''), 'unit')

  def test_empty_group_rejected(self):
    with self.assertRaises(ValueError):
      worker.parse_suite(self.valid().replace('PASS b', 'PASS '), 'unit')

  def test_boolean_count_rejected(self):
    raw = 'PASS a\n' + json.dumps({'passed': True, 'failed': 0, 'skipped': 0})
    with self.assertRaises(ValueError):
      worker.parse_suite(raw, 'unit')

  def test_missing_failure_field_rejected(self):
    with self.assertRaises(ValueError):
      worker.parse_suite('PASS a\n' + json.dumps({'passed': 1, 'skipped': 0}), 'unit')

  def test_skipped_group_rejected(self):
    with self.assertRaises(ValueError):
      worker.parse_suite(self.valid().replace('"skipped": 0', '"skipped": 1'), 'unit')

  def test_failed_group_rejected(self):
    with self.assertRaises(ValueError):
      worker.parse_suite(self.valid().replace('"failed": 0', '"failed": 1'), 'unit')

  def test_command_inventory_has_four_suites(self):
    for suite in ('native_usb', 'native_spi', 'native_spi_probe', 'native_binding'):
      self.assertIn('run-' + suite, worker.expected_commands())
    self.assertEqual(len(worker.expected_commands()), len(set(worker.expected_commands())))

  def test_linux_actual_main_is_offline_only(self):
    added = worker.expected_commands(True)[len(worker.expected_commands()):]
    self.assertEqual(added, ['link-actual-pandad-p9', 'actual-pandad-p9-offline', 'actual-pandad-p9-owner',
                             'deny---compat-native', 'deny---compat-runtime', 'deny---compat-device'])

  def test_native_suite_identity_and_scope_pass(self):
    for suite in worker.SUITES:
      summary = {'suite': 'p9_' + suite, 'passed': 1, 'failed': 0, 'skipped': 0, 'real_device_io': False}
      self.assertEqual(worker.parse_suite('PASS a\n' + json.dumps(summary), suite)['summary'], summary)

  def test_wrong_native_suite_identity_rejected(self):
    summary = {'suite': 'p9_native_spi', 'passed': 1, 'failed': 0, 'skipped': 0, 'real_device_io': False}
    with self.assertRaises(ValueError):
      worker.parse_suite('PASS a\n' + json.dumps(summary), 'native_usb')

  def test_native_hardware_claim_rejected(self):
    summary = {'suite': 'p9_native_usb', 'passed': 1, 'failed': 0, 'skipped': 0, 'real_device_io': True}
    with self.assertRaises(ValueError):
      worker.parse_suite('PASS a\n' + json.dumps(summary), 'native_usb')

  def test_native_missing_hardware_scope_rejected(self):
    summary = {'suite': 'p9_native_usb', 'passed': 1, 'failed': 0, 'skipped': 0}
    with self.assertRaises(ValueError):
      worker.parse_suite('PASS a\n' + json.dumps(summary), 'native_usb')

  def test_native_integer_false_scope_rejected(self):
    summary = {'suite': 'p9_native_usb', 'passed': 1, 'failed': 0, 'skipped': 0, 'real_device_io': 0}
    with self.assertRaises(ValueError):
      worker.parse_suite('PASS a\n' + json.dumps(summary), 'native_usb')


class FreshEvidenceTests(unittest.TestCase):
  def setUp(self):
    self.report = {'started_at_utc': '2026-10-18T01:00:00+00:00', 'completed_at_utc': '2026-10-18T02:00:00+00:00'}
    self.evidence = {'native': {'started_at': '2026-10-18T01:10:00+00:00', 'completed_at': '2026-10-18T01:50:00+00:00'},
                     'contract': {'started_at_utc': '2026-10-18T01:05:00+00:00', 'completed_at_utc': '2026-10-18T01:09:00+00:00'},
                     'replay-1': {'created_at_utc': '2026-10-18T01:01:00+00:00'},
                     'replay-2': {'created_at_utc': '2026-10-18T01:02:00+00:00'}}

  def test_same_batch_times_pass(self):
    core.validate_fresh_times(self.report, self.evidence)

  def test_old_contract_rejected(self):
    self.evidence['contract']['started_at_utc'] = '2026-10-17T01:05:00+00:00'
    with self.assertRaises(ValueError):
      core.validate_fresh_times(self.report, self.evidence)

  def test_future_contract_rejected(self):
    self.evidence['contract']['completed_at_utc'] = '2026-10-18T02:01:00+00:00'
    with self.assertRaises(ValueError):
      core.validate_fresh_times(self.report, self.evidence)

  def test_old_replay_rejected(self):
    for name in ('replay-1', 'replay-2'):
      with self.subTest(name=name):
        altered = copy.deepcopy(self.evidence)
        altered[name]['created_at_utc'] = '2026-10-17T01:01:00+00:00'
        with self.assertRaises(ValueError):
          core.validate_fresh_times(self.report, altered)

  def test_future_replay_rejected(self):
    self.evidence['replay-2']['created_at_utc'] = '2026-10-18T02:01:00+00:00'
    with self.assertRaises(ValueError):
      core.validate_fresh_times(self.report, self.evidence)

  def test_missing_replay_timestamp_rejected(self):
    del self.evidence['replay-1']['created_at_utc']
    with self.assertRaises(KeyError):
      core.validate_fresh_times(self.report, self.evidence)

  def test_reversed_child_interval_rejected(self):
    self.evidence['native']['started_at'] = '2026-10-18T01:55:00+00:00'
    with self.assertRaises(ValueError):
      core.validate_fresh_times(self.report, self.evidence)

  def test_reversed_batch_interval_rejected(self):
    self.report['started_at_utc'] = '2026-10-18T03:00:00+00:00'
    with self.assertRaises(ValueError):
      core.validate_fresh_times(self.report, self.evidence)

  def test_timezone_missing_rejected(self):
    self.evidence['replay-2']['created_at_utc'] = '2026-10-18T01:02:00'
    with self.assertRaises(ValueError):
      core.validate_fresh_times(self.report, self.evidence)


class ArtifactTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    (self.root / 'result.json').write_text('{}')
    (self.root / 'a.log').write_text('PASS A\n')
    self.report = {'retained_files_sha256': {'a.log': old.sha(self.root / 'a.log')}}

  def test_retained_artifacts_pass(self):
    evidence.check_retained(self.report, self.root)

  def test_changed_log_rejected(self):
    (self.root / 'a.log').write_text('different')
    with self.assertRaises(ValueError):
      evidence.check_retained(self.report, self.root)

  def test_extra_artifact_rejected(self):
    (self.root / 'unexpected.o').write_bytes(b'abc')
    with self.assertRaises(ValueError):
      evidence.check_retained(self.report, self.root)

  def test_missing_artifact_rejected(self):
    (self.root / 'a.log').unlink()
    with self.assertRaises(ValueError):
      evidence.check_retained(self.report, self.root)

  def test_symlink_artifact_rejected(self):
    (self.root / 'linked.log').symlink_to(self.root / 'a.log')
    with self.assertRaises(ValueError):
      evidence.check_retained(self.report, self.root)

  def test_new_python_module_enters_inventory(self):
    (self.root / 'native_maintenance').mkdir()
    (self.root / 'native_maintenance' / 'unreviewed.py').write_text('')
    self.assertIn('native_maintenance/unreviewed.py', core.protected_paths(self.root))

  def test_generated_bytecode_is_excluded(self):
    cache = self.root / 'native_maintenance/__pycache__'
    cache.mkdir(parents=True)
    (cache / 'generated.pyc').write_bytes(b'')
    self.assertFalse(any('__pycache__' in p for p in core.protected_paths(self.root)))

  def test_lock_cannot_hash_itself(self):
    (self.root / 'native_maintenance').mkdir()
    (self.root / core.LOCK).write_text('{}')
    self.assertNotIn(core.LOCK, core.protected_paths(self.root))


class CiTests(unittest.TestCase):
  def test_workflow_unchanged_template(self):
    ci.check_workflow(ci.ROOT)

  def test_workflow_does_not_install_dependencies(self):
    text = ci.workflow_text()
    self.assertNotIn('pip install', text)
    self.assertNotIn('apt-get', text)
    self.assertIn('unshare --user --map-root-user --net --mount --fork', text)
    self.assertIn('contents: read', text)

  def test_contract_inventory_fixed(self):
    seen = []
    ci.contract_commands(lambda name, argv: seen.append(name), 'python')
    self.assertEqual(seen, ci.contract_names())

  def test_full_only_accepts_blocked_candidate(self):
    gate = {'status': 'BLOCKED_FOR_DEPLOYMENT', 'offline_native_complete': True,
            'p8_prerequisite_verified': True, 'deployment_ready': False,
            'p8_arm_and_vehicle_newly_executed': False, 'deployment_blockers': ['physical identity']}
    seen = []
    def run(name, argv, expected=0):
      seen.append((name, expected))
      return json.dumps(gate)
    self.assertEqual(ci.full_commands(run, 'python', Path('/tmp/out'), 'local-vm', '/home/test/prepared'), gate)
    self.assertEqual(seen, [('verify-lock', 0), ('validate', 0), ('release-check', 3)])

  def test_full_rejects_deployable_claim(self):
    gate = {'status': 'BLOCKED_FOR_DEPLOYMENT', 'offline_native_complete': True,
            'p8_prerequisite_verified': True, 'deployment_ready': True,
            'p8_arm_and_vehicle_newly_executed': False, 'deployment_blockers': ['physical identity']}
    with self.assertRaises(ValueError):
      ci.full_commands(lambda *args, **kwargs: json.dumps(gate), 'python', Path('/tmp/out'), 'local-vm', '/home/test/prepared')

  def test_full_rejects_missing_blockers(self):
    gate = {'status': 'BLOCKED_FOR_DEPLOYMENT', 'offline_native_complete': True,
            'p8_prerequisite_verified': True, 'deployment_ready': False,
            'p8_arm_and_vehicle_newly_executed': False, 'deployment_blockers': []}
    with self.assertRaises(ValueError):
      ci.full_commands(lambda *args, **kwargs: json.dumps(gate), 'python', Path('/tmp/out'), 'local-vm', '/home/test/prepared')

  def test_full_rejects_historical_results_as_fresh(self):
    gate = {'status': 'BLOCKED_FOR_DEPLOYMENT', 'offline_native_complete': True,
            'p8_prerequisite_verified': True, 'deployment_ready': False,
            'p8_arm_and_vehicle_newly_executed': True, 'deployment_blockers': ['physical identity']}
    with self.assertRaises(ValueError):
      ci.full_commands(lambda *args, **kwargs: json.dumps(gate), 'python', Path('/tmp/out'), 'local-vm', '/home/test/prepared')

  def test_vm_name_injection_rejected(self):
    with self.assertRaises(ValueError):
      ci.full_commands(lambda *args, **kwargs: self.fail('must not execute'), 'python', Path('/tmp/out'), 'vm;command', '/home/test/prepared')

  def test_vm_root_escape_rejected(self):
    with self.assertRaises(ValueError):
      ci.full_commands(lambda *args, **kwargs: self.fail('must not execute'), 'python', Path('/tmp/out'), 'local-vm', '/home/../tmp')

  def test_failed_verification_stops_pipeline(self):
    seen = []
    def run(name, argv, expected=0):
      seen.append(name)
      raise ValueError('missing evidence')
    with self.assertRaises(ValueError):
      ci.full_commands(run, 'python', Path('/tmp/out'), 'local-vm', '/home/test/prepared')
    self.assertEqual(seen, ['verify-lock'])


class SourceTranscriptTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    (self.root / 'native_maintenance').mkdir()
    identity = 'test_fixture.Case.test_one'
    old.write_json(self.root / core.LOCK, {'required_tests': [identity]})
    transcript = 'test_one (' + identity + ') ... ok\n\n----------------------------------------------------------------------\nRan 1 test in 0.001s\n\nOK\n'
    self.report = {'schema': 'songplus-native-ci/v1', 'status': 'PASS', 'mode': 'contract',
                   'failed': 0, 'skipped': 0, 'passed_commands': len(ci.contract_names()), 'commands': [],
                   'device_access': False, 'deployment_ready': False, 'remote_ci_executed': False}
    def run(name, argv):
      row = {'name': name, 'argv': argv, 'returncode': 0, 'expected_returncode': 0}
      for stream in ('stdout', 'stderr'):
        path = self.root / (name + '.' + stream + '.log')
        path.write_text(transcript if name == 'native-tests' and stream == 'stderr' else '')
        row[stream + '_sha256'] = old.sha(path)
      if name == 'native-tests':
        row['tests_passed'] = 1
      self.report['commands'].append(row)
    ci.contract_commands(run, 'python')

  def check(self):
    return evidence.validate_contract(self.report, self.root, self.root)

  def test_exact_ci_transcript_passes(self):
    self.assertEqual(self.check()['maintenance_tests'], 1)

  def test_missing_command_rejected(self):
    self.report['commands'].pop()
    with self.assertRaises(ValueError):
      self.check()

  def test_different_command_argument_rejected(self):
    self.report['commands'][0]['argv'][-1] = '--allow-device'
    with self.assertRaises(ValueError):
      self.check()

  def test_nonzero_exit_cannot_redefine_success(self):
    self.report['commands'][0].update(returncode=1, expected_returncode=1)
    with self.assertRaises(ValueError):
      self.check()

  def test_changed_test_ids_rejected(self):
    old.write_json(self.root / core.LOCK, {'required_tests': ['test_fixture.Case.test_replaced']})
    with self.assertRaises(ValueError):
      self.check()

  def test_changed_test_log_rejected(self):
    (self.root / 'native-tests.stderr.log').write_text('Ran 1 test in 0.001s\n\nOK\n')
    with self.assertRaises(ValueError):
      self.check()

  def test_successful_count_without_executed_test_rejected(self):
    path = self.root / 'native-tests.stderr.log'
    path.write_text('Ran 1 test in 0.001s\n\nOK\n')
    self.report['commands'][-1]['stderr_sha256'] = old.sha(path)
    with self.assertRaises(ValueError):
      self.check()

  def test_hardware_permission_claim_rejected(self):
    self.report['device_access'] = True
    with self.assertRaises(ValueError):
      self.check()


class ParentGateSignatureTests(unittest.TestCase):
  """Exercise both real call sites against the frozen P8 callable signature."""
  class ReachedParent(Exception):
    pass

  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    old.write_json(self.root / 'prior.json', {'status': 'historical'})
    self.prior = {'report': 'prior.json', 'sha256': old.sha(self.root / 'prior.json')}
    old.write_json(self.root / core.LOCK, {'files': {}, 'prerequisite': self.prior})
    old.write_json(self.root / core.parent.LOCK, {})
    old.write_json(self.root / 'candidate.json', {
      'schema': 'songplus-native-p9-acceptance/v1', 'status': 'PASS_OFFLINE_NATIVE',
      'lock_sha256': old.sha(self.root / core.LOCK), 'parent_lock_sha256': old.sha(self.root / core.parent.LOCK),
      'prerequisite': self.prior, **core.SCOPE})

  def test_validation_calls_frozen_parent_signature(self):
    with patch.object(core, 'verify_lock'), \
         patch.object(core.old, 'capture', side_effect=[b'commit\n', (self.root / core.LOCK).read_bytes()]), \
         patch.object(core.parent, 'release_check', autospec=True, side_effect=self.ReachedParent) as gate:
      with self.assertRaises(self.ReachedParent):
        core.validate(self.root / 'out', 'vm', '/home/prepared', self.root)
      gate.assert_called_once_with((self.root / 'prior.json').resolve())

  def test_release_calls_frozen_parent_signature(self):
    with patch.object(core, 'verify_lock'), \
         patch.object(core.parent, 'release_check', autospec=True, side_effect=self.ReachedParent) as gate:
      with self.assertRaises(self.ReachedParent):
        core.release_check(self.root / 'candidate.json', self.root)
      gate.assert_called_once_with((self.root / 'prior.json').resolve())


if __name__ == '__main__':
  unittest.main()
