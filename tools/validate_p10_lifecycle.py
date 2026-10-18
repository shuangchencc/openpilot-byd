#!/usr/bin/env python3
"""Fresh P0-P10 replay and Mac/Linux native provider regression. No hardware access."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def utc(): return datetime.now(timezone.utc).isoformat()
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--linux-instance',required=True);p.add_argument('--linux-root',default='/home/shuang.guest/songplus-p2-build');a=p.parse_args()
 if platform.system()!='Darwin': raise ValueError('Mac orchestrator required; workers support Linux')
 if not a.linux_instance or not all(c.isalnum() or c in '_-' for c in a.linux_instance):raise ValueError('invalid local VM instance')
 if not a.linux_root.startswith('/') or '..' in Path(a.linux_root).parts or not all(c.isalnum() or c in '/_.-' for c in a.linux_root): raise ValueError('invalid local VM directory')
 output=a.output.absolute()
 if any(q.is_symlink() for q in [output,*output.parents]): raise ValueError('output path symlink forbidden')
 output=output.resolve();output.mkdir(parents=True,exist_ok=False);started=utc();commands=[]
 protected=[ROOT/'tools'/name for name in ('validate_p10_lifecycle.py','build_p10_lifecycle.py','build_p9_native.py','build_p8_runtime.py','run_panda_p6_host.py','build_panda_daemon_p6.py','p8_runtime_linux_sandbox.sh','panda_firmware_offline.sb')]+list((ROOT/'patches/openpilot/p8').glob('*'))+list((ROOT/'patches/openpilot/p9').glob('*'))+list((ROOT/'patches/openpilot/p10').glob('*'))+[ROOT/'patches/openpilot/p6/manifest.json',ROOT/'patches/openpilot/p4/host-dependencies.json']
 protected={str(q.relative_to(ROOT)):digest(q) for q in protected if q.is_file()}
 source=output/'source';source.mkdir();manifest_path=ROOT/'patches/openpilot/p10/manifest.json';parent_path=ROOT/'patches/openpilot/p9/manifest.json';prior_path=ROOT/'patches/openpilot/p6/manifest.json';paths=[prior_path,ROOT/'patches/openpilot/p8/manifest.json',parent_path,manifest_path];chain=[json.loads(path.read_text()) for path in paths];prior=chain[0];parent=chain[-2];manifest=chain[-1];deps=json.loads((ROOT/'patches/openpilot/p4/host-dependencies.json').read_text())
 def run(name,argv,cwd=ROOT,env=None,timeout=1200,expected=0):
  argv=list(map(str,argv));r=subprocess.run(argv,cwd=cwd,env=env,capture_output=True,timeout=timeout)
  for stream,data in [('stdout',r.stdout),('stderr',r.stderr)]: (output/(name+'.'+stream)).write_bytes(data)
  commands.append({'name':name,'argv':argv,'exit_code':r.returncode,'expected_exit_code':expected,'stdout_sha256':digest(output/(name+'.stdout')),'stderr_sha256':digest(output/(name+'.stderr'))})
  if r.returncode!=expected:raise RuntimeError(name+' failed: '+r.stderr.decode()[-3000:])
  return r.stdout.decode()
 try:
  for index in range(1,len(chain)):
   if chain[index]['parent_manifest_sha256']!=digest(paths[index-1]) or chain[index]['baselines']!=prior['baselines']:raise ValueError('P10 parent/baseline mismatch')
  inputs=dict(prior['baseline_inputs'])
  for item in chain[1:]:inputs.update(item.get('additional_baseline_inputs',{}))
  for relative,expected in inputs.items():
   inp=ROOT/relative
   if digest(inp)!=expected:raise ValueError('baseline drift '+relative)
   dst=source/relative.removeprefix('upstream-openpilot/');dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(inp.read_bytes())
  run('git-init',['git','init','-q'],source)
  for index,(directory,item) in enumerate([(path.parent,x) for path,entry in zip(paths,chain) for x in entry['series']]):
   patch=(directory/item['file']).resolve()
   if not patch.is_relative_to(ROOT/'patches/openpilot') or digest(patch)!=item['sha256']:raise ValueError('patch drift')
   run('apply-'+str(index),['git','apply',patch],source)
  expected={name.removeprefix('upstream-openpilot/'):hash for name,hash in inputs.items()}
  for entry in chain:expected.update(entry['result_files'])
  actual={str(q.relative_to(source)):digest(q) for q in source.rglob('*') if q.is_file() and '.git' not in q.parts}
  if actual!=expected:raise ValueError('replayed tree mismatch')
  mac=deps['macos'];prefix=Path(deps['daemon']['macos']['prefix'])
  for item in mac['files']+deps['daemon']['macos']['files']:
   if digest(Path(item['path']))!=item['sha256']:raise ValueError('cached dependency drift')
  env=dict(os.environ,P2_NETWORK_DEVICE_SANDBOX='macos-seatbelt',PYTHONDONTWRITEBYTECODE='1',UV_OFFLINE='1',PIP_NO_INDEX='1')
  run('macos',['/usr/bin/sandbox-exec','-f',ROOT/'tools/panda_firmware_offline.sb',sys.executable,'-S',ROOT/'tools/build_p10_lifecycle.py','--source',source,'--output',output/'macos','--prefix',prefix,'--compiler','clang++','--usb-include',mac['usb_include'],'--usb-library',mac['usb_library']],env=env)
  bundle=output/'input.tar'
  with tarfile.open(bundle,'w') as tar:
   for q in sorted(source.rglob('*')):
    if q.is_file() and '.git' not in q.parts:tar.add(q,arcname='source/'+str(q.relative_to(source)))
   for name in ('build_p10_lifecycle.py','build_p9_native.py','build_p8_runtime.py','run_panda_p6_host.py','build_panda_daemon_p6.py','p8_runtime_linux_sandbox.sh'):tar.add(ROOT/'tools'/name,arcname=name)
   tar.add(ROOT/'patches/openpilot/p4/host-dependencies.json',arcname='dependencies.json')
  remote=a.linux_root.rstrip('/')+'/p10-lifecycle-'+uuid.uuid4().hex
  run('vm-mkdir',['limactl','shell',a.linux_instance,'--','mkdir','-p',remote])
  run('vm-copy',['limactl','copy',bundle,a.linux_instance+':'+remote+'/input.tar'])
  observed=run('vm-bundle-hash',['limactl','shell',a.linux_instance,'--','sha256sum',remote+'/input.tar']).split()[0]
  if observed!=digest(bundle):raise ValueError('Linux input copy drift')
  run('vm-extract',['limactl','shell',a.linux_instance,'--','tar','-xf',remote+'/input.tar','-C',remote])
  linux_prefix=deps['daemon']['linux']['prefix'];usb_prefix=a.linux_root.rstrip('/')+'/deps/prefix'
  check="import json,hashlib,pathlib,sys; j=json.loads(pathlib.Path(sys.argv[1]).read_text()); items=[(pathlib.Path(j['daemon']['linux']['prefix'])/x['path'],x['sha256']) for x in j['daemon']['linux']['files']]+[(pathlib.Path(sys.argv[2])/x['path'],x['sha256']) for x in j['linux']['files']]; valid=all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in items); sys.exit(0 if valid else 'cached Linux dependency drift')"
  run('vm-dependencies',['limactl','shell',a.linux_instance,'--','python3','-S','-c',check,remote+'/dependencies.json',usb_prefix])
  run('linux',['limactl','shell',a.linux_instance,'--','sh',remote+'/p8_runtime_linux_sandbox.sh','python3','-S',remote+'/build_p10_lifecycle.py','--source',remote+'/source','--output',remote+'/output','--prefix',linux_prefix,'--compiler','g++','--usb-include',usb_prefix+'/usr/include/libusb-1.0','--usb-library',usb_prefix+'/usr/lib/aarch64-linux-gnu/libusb-1.0.so.0.3.0'])
  run('vm-dependencies-after',['limactl','shell',a.linux_instance,'--','python3','-S','-c',check,remote+'/dependencies.json',usb_prefix])
  run('vm-result-copy',['limactl','copy','-r',a.linux_instance+':'+remote+'/output',output/'linux'])
  reports={label:json.loads((output/label/'result.json').read_text()) for label in ('macos','linux')}
  for report in reports.values():
   if report['status']!='PASS_OFFLINE_HOST_ONLY' or report['source_files_sha256']!=actual:raise ValueError('worker/source mismatch')
  for label,r in reports.items():
   if r['groups']!=manifest['required_groups'][label]: raise ValueError('required native groups mismatch')
  if {str(q.relative_to(source)):digest(q) for q in source.rglob('*') if q.is_file() and '.git' not in q.parts}!=actual: raise ValueError('parent replay source drift during validation')
  for item in mac['files']+deps['daemon']['macos']['files']:
   if digest(Path(item['path']))!=item['sha256']: raise ValueError('cached dependency drift after validation')
  if any(digest(ROOT/name)!=expected for name,expected in protected.items()): raise ValueError('validation inputs drift during execution')
  report={'schema':'songplus-lifecycle-p10-validation/v1','status':'PASS_OFFLINE_HOST_ONLY','started_at':started,'completed_at':utc(),'commands':commands,'manifest_sha256':digest(manifest_path),'parent_manifest_sha256':digest(parent_path),'source_files_sha256':actual,'patches':manifest['series'],'platforms':{name:{'summary':r['summary'],'groups':r['groups'],'result_sha256':digest(output/name/'result.json')} for name,r in reports.items()},'real_device_discovery':False,'real_device_io':False,'real_can_tx':False,'deployment_ready':False,'profile_bound_to_real_hardware':False,'linux_input_sha256':digest(bundle),'worker_sha256':digest(ROOT/'tools/build_p10_lifecycle.py'),'validation_inputs_sha256':protected}
  (output/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'status':report['status'],'platforms':report['platforms']}))
 except Exception as exc:
  (output/'result.json').write_text(json.dumps({'status':'FAIL','started_at':started,'completed_at':utc(),'error':str(exc),'commands':commands},indent=2)+'\n');raise
if __name__=='__main__':main()
