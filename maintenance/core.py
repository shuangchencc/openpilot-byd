"""Local-only maintenance primitives. No transport or device imports."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = 'maintenance/downstream-lock.json'
HOST = 'patches/openpilot/p6/manifest.json'
PANDA = 'patches/panda/p5/manifest.json'
DESIGN = 'hardware/design/dos-usb-dual-panda/offline-acceptance.json'


def require(ok, message):
  if not ok:
    raise ValueError(message)


def sha(path):
  return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
  def unique(pairs):
    result = {}
    for key, value in pairs:
      require(key not in result, 'Duplicate JSON key: ' + key)
      result[key] = value
    return result
  return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def write_json(path, value):
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def safe_path(root, name):
  root = Path(root).resolve()
  rel = Path(name)
  require(not rel.is_absolute() and rel.parts and '..' not in rel.parts, 'Unsafe relative path: ' + str(name))
  path = root / rel
  require(path.resolve().is_relative_to(root), 'Path escapes root: ' + str(name))
  require(not any(p.is_symlink() for p in [path, *path.parents] if p != root and p.is_relative_to(root)),
          'Symlink forbidden: ' + str(name))
  return path


def tree_hashes(root):
  root = Path(root)
  result = {}
  for path in sorted(root.rglob('*')):
    if '.git' in path.relative_to(root).parts:
      continue
    require(not path.is_symlink(), 'Source symlink forbidden: ' + str(path))
    if path.is_file():
      result[str(path.relative_to(root))] = sha(path)
  return result


def capture(argv, cwd=ROOT):
  env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
             GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1')
  return subprocess.run(list(map(str, argv)), cwd=cwd, env=env, check=True,
                        capture_output=True, timeout=120).stdout


def normalized_series(root, manifest_path):
  manifest = read_json(root / manifest_path)
  series = []
  for item in manifest['series']:
    file = (root / manifest_path).parent / item['file']
    require(file.resolve().is_relative_to(root / 'patches'), 'Patch path escape')
    name = str(file.resolve().relative_to(root))
    file = safe_path(root, name)
    require(sha(file) == item['sha256'], 'Patch drift: ' + name)
    header = file.read_bytes().splitlines()
    match = re.fullmatch(rb'From ([0-9a-f]{40}) Mon Sep 17 00:00:00 2001', header[0]) if header else None
    require(match is not None and match.group(1).decode() == item['commit'], 'Patch commit mismatch: ' + name)
    series.append({**item, 'file': name})
  return series


def verify_lock(root=ROOT, metadata_only=False):
  root = Path(root).resolve()
  lock = read_json(root / LOCK)
  require(lock['schema'] == 'songplus-maintenance-lock/v1', 'Unsupported downstream lock')
  require(lock['device_access'] is False and lock['deployment_ready'] is False, 'Invalid lock scope')
  required = {HOST, PANDA, DESIGN, 'upstream-lock.json', 'tools/songplus_compat.py',
              'maintenance/core.py', 'maintenance/validation.py', 'maintenance/runtime_guard.py', 'maintenance/child_evidence.py',
              'maintenance/reproducibility.py', 'maintenance/release_policy.json',
              'tools/validate_panda_p6.py', 'tools/validate_panda_firmware.py',
              'tools/check_panda_ci.py', '.github/workflows/panda-compat-offline.yml'}
  require(required <= set(lock['files']), 'Required locked file absent')
  for name, expected in lock['files'].items():
    require(sha(safe_path(root, name)) == expected, 'Locked file drift: ' + name)
  host, panda = read_json(root / HOST), read_json(root / PANDA)
  upstream = {r['path']: r['commit'] for r in read_json(root / 'upstream-lock.json')['repos']}
  require(lock['baselines'] == host['baselines'] == panda['baselines'], 'Baseline manifest disagreement')
  require(all(upstream.get(p) == commit for p, commit in lock['baselines'].items()), 'Upstream pin drift')
  require(lock['series']['host'] == normalized_series(root, HOST), 'Host patch ordering drift')
  require(lock['series']['panda'] == normalized_series(root, PANDA), 'Panda patch ordering drift')
  require(lock['series']['opendbc'] == [], 'Unexpected safety/vehicle patch')
  require([p['stage'] for p in lock['series']['host']] == ['P0', 'P1', 'P2', 'P3', 'P4', 'P6'], 'Invalid stage ordering')
  require(host['p5_manifest_sha256'] == sha(root / PANDA), 'P6/P5 chain drift')
  prior = 'patches/openpilot/p4/manifest.json'
  for current, previous in [(HOST, prior), (prior, 'patches/openpilot/p3/manifest.json'),
                            ('patches/openpilot/p3/manifest.json', 'patches/openpilot/p2/manifest.json'),
                            ('patches/openpilot/p2/manifest.json', 'patches/openpilot/p0p1/manifest.json')]:
    require(read_json(root / current)['depends_on_manifest_sha256'] == sha(root / previous), 'Prior patch manifest drift')
  require(panda['host_p0_p4_manifest_sha256'] == sha(root / prior), 'Firmware host dependency drift')
  cases = [c['id'] for c in read_json(root / DESIGN)['cases'] if c['required']]
  require(lock['required_case_ids'] == cases and len(set(cases)) == 41, 'Frozen acceptance IDs drift')
  for series in lock['series'].values():
    for patch in series:
      require(lock['files'].get(patch['file']) == patch['sha256'], 'Patch omitted from lock files')
  checks = {'metadata_files': len(lock['files']), 'source_files': 0, 'dependency_files': 0}
  if not metadata_only:
    for name, commit in lock['baselines'].items():
      require(capture(['git', 'rev-parse', 'HEAD'], root / name).decode().strip() == commit, 'Checkout pin drift: ' + name)
    for name in ('panda', 'opendbc_repo', 'msgq_repo'):
      entry = capture(['git', 'ls-tree', lock['baselines']['upstream-openpilot'], name], root / 'upstream-openpilot').decode().split()
      require(len(entry) == 4 and entry[:2] == ['160000', 'commit'] and entry[2] == lock['baselines']['upstream-openpilot/' + name], 'Gitlink drift: ' + name)
    require(all(panda['baseline_inputs'][p] == h for p, h in host['baseline_inputs'].items() if p in panda['baseline_inputs']), 'Conflicting baseline input hashes')
    inputs = {**host['baseline_inputs'], **panda['baseline_inputs']}
    for name, expected in inputs.items():
      require(sha(safe_path(root, name)) == expected, 'Baseline input drift: ' + name)
    deps = {str(root / p): h for p, h in panda['dependencies'].items()}
    for path in ('patches/openpilot/p2/host-dependencies.json', 'patches/openpilot/p4/host-dependencies.json'):
      dep = read_json(root / path)
      for section in [dep['macos'], dep.get('daemon', {}).get('macos', {})]:
        deps.update({f['path']: f['sha256'] for f in section.get('files', [])})
    for name, expected in deps.items():
      require(sha(name) == expected, 'Cached dependency drift/missing: ' + name)
    python = root / 'upstream-openpilot/.venv/bin/python'
    expected_version = read_json(root / 'upstream-lock.json')['python_dependencies']['python_version']
    require(capture([python, '--version']).decode().strip() == 'Python ' + expected_version, 'Locked Python version drift')
    checks.update(source_files=len(inputs), dependency_files=len(deps))
  return {'status': 'PASS', 'metadata_only': metadata_only, 'lock_sha256': sha(root / LOCK),
          'checks': checks, 'device_access': False, 'deployment_ready': False}


def new_output(path):
  path = Path(os.path.abspath(path))
  require(not path.exists() and not path.is_symlink(), 'Output must be a new directory: ' + str(path))
  require(not any(p.is_symlink() for p in path.parents), 'Output parent symlink forbidden')
  path.mkdir(parents=True)
  return path


def archive(repo, commit, dest):
  dest.mkdir()
  data = capture(['git', 'archive', commit], repo)
  with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    seen = set()
    for member in tar:
      target = safe_path(dest, member.name)
      require(target not in seen, 'Duplicate archive member: ' + member.name)
      seen.add(target)
      if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
      else:
        require(member.isfile(), 'Git archive contains nonregular entry')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(tar.extractfile(member).read())
        target.chmod(member.mode & 0o777)


def replay(output, root=ROOT):
  root = Path(root).resolve()
  verified = verify_lock(root)
  out = new_output(output)
  host, panda = read_json(root / HOST), read_json(root / PANDA)
  hostdir = out / 'host'
  hostdir.mkdir()
  for name, expected in host['baseline_inputs'].items():
    target = safe_path(hostdir, name.removeprefix('upstream-openpilot/'))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(safe_path(root, name).read_bytes())
    require(sha(target) == expected, 'Copied source changed')
  archive(root / 'upstream-openpilot/panda', panda['baselines']['upstream-openpilot/panda'], out / 'panda')
  archive(root / 'upstream-openpilot/opendbc_repo', panda['baselines']['upstream-openpilot/opendbc_repo'], out / 'opendbc')
  commands = []
  for name, manifest in [('host', host), ('panda', panda)]:
    directory = out / name
    command = ['git', '-c', 'init.templateDir=', 'init', '-q', directory]
    capture(command); commands.append(list(map(str, command)))
    series = normalized_series(root, HOST if name == 'host' else PANDA)
    for patch in series:
      for action in [['--check'], []]:
        command = ['git', 'apply', *action, root / patch['file']]
        capture(command, directory); commands.append(list(map(str, command)))
    expected = ({k.removeprefix('upstream-openpilot/'): v for k, v in host['baseline_inputs'].items()}
                if name == 'host' else {})
    expected.update(manifest['result_files'] if name == 'host' else manifest['result_tree'])
    require(tree_hashes(directory) == expected, 'Complete replay tree mismatch: ' + name)
  trees = {name: tree_hashes(out / name) for name in ['host', 'panda', 'opendbc']}
  # Path/mtime/uid-independent source bundle; no .git, generated artifacts or private inputs.
  bundle = out / 'source.tar'
  with tarfile.open(bundle, 'w', format=tarfile.USTAR_FORMAT) as tar:
    for name, entries in trees.items():
      for path in sorted(entries):
        data = (out / name / path).read_bytes()
        info = tarfile.TarInfo(name + '/' + path)
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        tar.addfile(info, io.BytesIO(data))
  result = {'status': 'PASS', 'lock_sha256': verified['lock_sha256'], 'baselines': host['baselines'],
            'files': trees, 'commands': commands, 'bundle_sha256': sha(bundle),
            'source_scope': 'P6 complete declared host build inputs; P5 full Panda; unmodified full opendbc',
            'device_access': False, 'deployment_ready': False}
  write_json(out / 'replay.json', result)
  return result
