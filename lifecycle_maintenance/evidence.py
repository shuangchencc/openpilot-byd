"""Revalidate P10 transcripts/artifacts and fresh nested P0–P9 host results."""
from __future__ import annotations
import json
from pathlib import Path
import re
from maintenance import core as old
from maintenance.integration_evidence import check_commands, groups_passed
from native_maintenance.evidence import check_retained, validate_p8_worker
from tools import build_p9_native as p9
from tools import build_p10_lifecycle as worker_api
from lifecycle_maintenance.core import ROOT, MANIFEST, PARENT, ANCESTOR, SCOPE, expected_source, require, read_json, sha, safe_path


def validate_worker(worker, folder, platform, sources, root, *, lifecycle):
  api = worker_api if lifecycle else p9
  stage = 'p10' if lifecycle else 'p9'
  manifest = read_json(root / (MANIFEST if lifecycle else PARENT))
  require(worker['schema'] == ('songplus-lifecycle-p10-worker/v1' if lifecycle else 'songplus-native-p9-worker/v1')
          and worker['status'] == 'PASS_OFFLINE_HOST_ONLY', 'Host worker failed')
  check_retained(worker, folder)
  require(worker['source_files_sha256'] == sources, 'Nested worker used different source')
  require(all(worker[k] is v for k, v in SCOPE.items()), 'Worker hardware permission claim')
  require(worker['sanitizers'] == ['address','undefined'] and worker['provider_calls_injected'] is True
          and worker['native_usb_compiled_and_linked'] is True
          and worker['native_spi_linux_ioctls_compiled'] is (platform == 'linux')
          and worker['isolated_msgq_endpoints_opened'] is (platform == 'linux')
          and worker['network_sockets_opened'] is False, 'Worker isolation/compiled provider proof missing')
  deps = worker['native_dependencies_sha256']
  require(worker['native_dependencies_unchanged'] is True and isinstance(deps,dict) and len(deps)>20, 'Dependency provenance missing')
  require(all(Path(name).is_absolute() and re.fullmatch(r'[0-9a-f]{64}', value) for name,value in deps.items()), 'Invalid dependency hash')
  for name in (*api.SOURCES, *('tests/test_'+suite+'.cc' for suite in api.SUITES)):
    suffix = 'openpilot/selfdrive/pandad/compatibility/'+name
    require([v for p,v in deps.items() if p.endswith('/'+suffix)] == [sources[suffix]], 'Incomplete dependency scan')
  if platform == 'linux' and not lifecycle:
    require(any(p.endswith('/linux/spi/spidev.h') for p in deps), 'Missing native spidev header provenance')
  require(worker['groups'] == manifest['required_groups'][platform], 'Host group inventory changed')
  failures = dict.fromkeys(('deny---compat-native','deny---compat-runtime','deny---compat-device'), 2) if platform == 'linux' else {}
  if lifecycle and platform == 'linux': failures.update({'deny-default':3,'deny-serial':3,'deny-profile-environment':2})
  check_commands(worker['commands'], folder, set(api.expected_commands(platform == 'linux')), failures)
  require([c['name'] for c in worker['commands']] == api.expected_commands(platform == 'linux'), 'Worker command ordering changed')
  require(set(worker['suites']) == set(api.SUITES), 'Missing suite')
  groups=[]
  for suite in api.SUITES:
    observed = worker['suites'][suite]
    parsed = api.parse_suite((folder/('run-'+suite+'.stdout')).read_text(), suite)
    require(parsed['summary'] == observed['summary'] and parsed['groups'] == observed['groups'], 'Suite transcript mismatch')
    require(sha(folder/('test_'+suite)) == observed['binary_sha256'], 'Suite binary changed')
    groups.extend(parsed['groups'])
  require(groups == worker['groups'], 'Merged group inventory changed')
  count=groups_passed(worker)
  require(worker['actual_pandad_relinked_with_native_providers'] is (platform=='linux'), 'Actual main linkage missing')
  if platform=='linux':
    main=api.parse_suite((folder/('actual-pandad-'+stage+'-owner.stdout')).read_text(),stage+'-main')
    require(main == worker['actual_main'] and main['groups']==['p8_actual_main_unbound_owner_no_io']
            and main['summary']['real_device_io'] is False
            and sha(folder/('pandad-'+stage))==worker['actual_main_binary_sha256'], 'Actual main owner proof changed')
  else:
    require(worker['actual_main'] is None and worker['actual_main_binary_sha256'] is None, 'Invalid Mac actual main claim')
  if lifecycle:
    for name in ('tests/spi_freshness_firmware_oracle.c','tests/spi_freshness_oracle/board/drivers/drivers.h'):
      suffix='openpilot/selfdrive/pandad/compatibility/'+name
      require([v for p,v in deps.items() if p.endswith('/'+suffix)]==[sources[suffix]], 'Firmware oracle input provenance missing')
    driver='panda/board/drivers/spi.h'
    require(worker['locked_spi_driver_sha256']==sources[driver]
            and [v for p,v in deps.items() if p.endswith('/'+driver)]==[sources[driver]]
            and sha(folder/'firmware-oracle.o')==worker['firmware_oracle_sha256'], 'Locked firmware oracle changed')
    if platform=='linux':
      gate=api.parse_suite((folder/'actual-pandad-p10-gate.stdout').read_text(),'p10-main-gate')
      require(gate==worker['production_check'] and gate['groups']==['p10_main_production_check_defaults_blocked']
              and gate['summary']['real_device_io'] is False, 'Actual launch gate diagnostic changed')
      for name in ('deny-default','deny-serial'):
        denied=json.loads((folder/(name+'.stdout')).read_text().splitlines()[-1])
        require(denied['entry']=='production_denied' and all(denied[k] is False for k in
                ('real_device_io','ready_for_review','profile_bound','production_start_authorized','runtime_writes_authorized','can_tx_authorized','deployment_authorized')) and bool(denied['blockers']), 'Default launch denial changed')
    else:
      require(worker['production_check'] is None, 'Mac actual gate claim invalid')
    path=folder/'p9-regression/result.json'
    require(sha(path)==worker['p9_regression_sha256'] and read_json(path)==worker['p9_regression'], 'Nested P9 regression changed')
    parent=validate_worker(worker['p9_regression'],path.parent,platform,sources,root,lifecycle=False)
    return {'p10_groups':count, **parent}
  path=folder/'p8-regression/result.json'
  require(sha(path)==worker['p8_regression_sha256'] and read_json(path)==worker['p8_regression'], 'Nested P8 regression changed')
  return {'p9_groups':count,'p8_runtime_groups':validate_p8_worker(worker['p8_regression'],path.parent,platform,sources,root)}


def validate_native_report(report, base, root=ROOT):
  root,base=Path(root),Path(base)
  manifest=read_json(root/MANIFEST)
  require(report['schema']=='songplus-lifecycle-p10-validation/v1' and report['status']=='PASS_OFFLINE_HOST_ONLY', 'Lifecycle build incomplete')
  require(report['manifest_sha256']==sha(root/MANIFEST) and report['parent_manifest_sha256']==sha(root/PARENT)
          and report['patches']==manifest['series'], 'P10 patch identity changed')
  require(all(report[k] is v for k,v in SCOPE.items()), 'Validation scope changed')
  require(report['worker_sha256']==sha(root/'tools/build_p10_lifecycle.py'), 'P10 worker changed')
  required={'tools/validate_p10_lifecycle.py','tools/build_p10_lifecycle.py','tools/build_p9_native.py','tools/build_p8_runtime.py',
            'tools/run_panda_p6_host.py','tools/build_panda_daemon_p6.py','tools/p8_runtime_linux_sandbox.sh','tools/panda_firmware_offline.sb',
            'patches/openpilot/p6/manifest.json','patches/openpilot/p4/host-dependencies.json'}
  for directory in ('patches/openpilot/p8','patches/openpilot/p9','patches/openpilot/p10'):
    required.update(str(p.relative_to(root)) for p in (root/directory).iterdir() if p.is_file())
  require(set(report['validation_inputs_sha256'])==required,'Validation input inventory changed')
  for name,value in report['validation_inputs_sha256'].items():require(sha(safe_path(root,name))==value,'Validation input drift: '+name)
  sources=expected_source(root)
  require(report['source_files_sha256']==sources==old.tree_hashes(base/'source'), 'P10 replay source changed')
  require(sha(base/'input.tar')==report['linux_input_sha256'],'Linux bundle changed')
  patches=sum(len(read_json(root/p)['series']) for p in ('patches/openpilot/p6/manifest.json',ANCESTOR,PARENT,MANIFEST))
  names={'git-init','macos','vm-mkdir','vm-copy','vm-bundle-hash','vm-extract','vm-dependencies','linux','vm-dependencies-after','vm-result-copy'}
  names.update('apply-'+str(i) for i in range(patches))
  check_commands(report['commands'],base,names)
  require(set(report['platforms'])=={'macos','linux'},'Both platforms required')
  counts={'platforms':{},'fresh_native_spi_regression_platforms':2,'actual_p10_main_gate_platforms':1,'failed':0,'skipped':0}
  for platform in ('macos','linux'):
    item=report['platforms'][platform];folder=base/platform
    require(sha(folder/'result.json')==item['result_sha256'],'Worker report changed')
    worker=read_json(folder/'result.json')
    require(worker['groups']==item['groups'] and worker['summary']==item['summary'],'Worker summary changed')
    counts['platforms'][platform]=validate_worker(worker,folder,platform,sources,root,lifecycle=True)
  return counts


def validate_contract(report, base, root=ROOT):
  from tools.check_songplus_lifecycle_ci import contract_names
  require(report["schema"] == "songplus-lifecycle-ci/v1" and report["status"] == "PASS" and report["mode"] == "contract", "Native source CI failed")
  require(report["failed"] == report["skipped"] == 0 and report["passed_commands"] == len(contract_names()), "Native source CI accounting invalid")
  require(report["device_access"] is False and report["deployment_ready"] is False and report["remote_ci_executed"] is False, "Source CI scope changed")
  require([c["name"] for c in report["commands"]] == contract_names(), "Source CI command order changed")
  commands = {"native-metadata": ["-B", "tools/songplus_lifecycle.py", "verify-lock", "--metadata-only"],
              "help-root": ["-B", "tools/songplus_lifecycle.py", "--help"],
              **{"help-" + name: ["-B", "tools/songplus_lifecycle.py", name, "--help"] for name in ("verify-lock", "replay", "validate", "release-check")},
              "native-tests": ["-B", "-m", "unittest", "discover", "-s", "lifecycle_maintenance", "-p", "test_*.py", "-v"]}
  python = None
  for command in report["commands"]:
    name, argv = command["name"], command["argv"]
    require(isinstance(argv, list) and len(argv) > 1 and isinstance(argv[0], str) and argv[0]
            and argv[1:] == commands[name], "Source CI argument inventory changed")
    python = argv[0] if python is None else python
    require(python == argv[0] and type(command["returncode"]) is int and type(command["expected_returncode"]) is int
            and command["returncode"] == command["expected_returncode"] == 0, "Source CI command failed")
    for stream in ("stdout", "stderr"):
      require(sha(safe_path(base, name + "." + stream + ".log")) == command[stream + "_sha256"], "Source CI transcript changed")
  import re
  transcript = (Path(base) / "native-tests.stderr.log").read_text()
  match = re.search(r"Ran (\d+) tests? in", transcript)
  require(match is not None and int(match.group(1)) > 0 and "\nOK\n" in transcript
          and "skipped=" not in transcript, "Native maintenance unittest transcript invalid")
  from lifecycle_maintenance.core import LOCK
  locked = read_json(Path(root) / LOCK)["required_tests"]
  executed = re.findall(r"^\S+ \(([^)]+)\) \.\.\. ok$", transcript, re.MULTILINE)
  require(len(executed) == len(set(executed)) == int(match.group(1)) and sorted(executed) == locked,
          "Native maintenance executed test IDs changed")
  require(report["commands"][-1]["tests_passed"] == len(locked), "Native CI claimed count changed")
  return {"commands": len(contract_names()), "maintenance_tests": int(match.group(1)), "failed": 0, "skipped": 0}
