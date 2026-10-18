#!/usr/bin/env python3
"""Build P8 on the replayed P6 tree. Caller supplies device/network sandbox."""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('source','output','prefix','usb-include','usb-library'): p.add_argument('--'+name,type=Path,required=True)
 p.add_argument('--compiler',default='clang++');a=p.parse_args()
 if not os.environ.get('P2_NETWORK_DEVICE_SANDBOX'): raise ValueError('offline sandbox required')
 if any(q.is_symlink() for q in [a.output.absolute(),*a.output.absolute().parents]): raise ValueError('output path symlink forbidden')
 a.source=a.source.resolve();a.output=a.output.resolve();a.prefix=a.prefix.resolve();a.usb_include=a.usb_include.resolve();a.usb_library=a.usb_library.resolve();a.output.mkdir(parents=True,exist_ok=False)
 pkg=a.source/'openpilot/selfdrive/pandad/compatibility';linux=platform.system()=='Linux'
 initial_sources={str(q.relative_to(a.source)):digest(q) for q in a.source.rglob('*') if q.is_file() and '.git' not in q.parts}
 commands=[];env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0:halt_on_error=1',UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
 def run(name,argv,timeout=1200,expected=0):
  argv=list(map(str,argv));r=subprocess.run(argv,cwd=a.source,env=env,capture_output=True,timeout=timeout)
  for stream,data in [('stdout',r.stdout),('stderr',r.stderr)]: (a.output/(name+'.'+stream)).write_bytes(data)
  commands.append({'name':name,'argv':argv,'exit_code':r.returncode,'expected_exit_code':expected,'stdout_sha256':digest(a.output/(name+'.stdout')),'stderr_sha256':digest(a.output/(name+'.stderr'))})
  if r.returncode!=expected: raise RuntimeError(name+' failed: '+r.stderr.decode()[-2000:])
  return r.stdout.decode()
 try:
  here=Path(__file__).resolve().parent
  build_raw=run('build-contract',[sys.executable,'-S',pkg/'tests/test_p8_build_contract.py'])
  build_contract=json.loads(build_raw.splitlines()[-1])
  if build_contract.get('passed')!=2 or build_contract.get('failed') or build_contract.get('skipped'): raise ValueError('SConscript contract failed')
  native=a.output/'native-regression'
  run('native-regression',[sys.executable,'-S',here/'run_panda_p6_host.py','--source',a.source,'--output',native,'--compiler',a.compiler,'--usb-include',a.usb_include,'--usb-library',a.usb_library])
  daemon=a.output/'daemon-regression'
  run('daemon-regression',[sys.executable,'-S',here/'build_panda_daemon_p6.py','--source',a.source,'--output',daemon,'--prefix',a.prefix,'--compiler',a.compiler,*([] if linux else ['--entry-test',pkg/'tests/test_pandad_runtime.cc'])])
  generated=daemon/'generated';cpp=generated/'openpilot/cereal/gen/cpp'
  flags=['-std=c++17','-Wall','-Wextra','-Werror','-O1','-g','-fno-omit-frame-pointer','-fsanitize=address,undefined','-pthread','-D__COMMA_HARDWARE__=0']
  for include in (generated,generated/'openpilot',cpp,a.source,a.source/'openpilot',pkg,a.source/'msgq_repo',a.source/'opendbc_repo',a.usb_include): flags+=['-I',include]
  flags+=['-isystem',a.prefix/'capnproto/install/include']
  new_sources=['runtime_integration.cc','realtime_bridge.cc','usb_backend.cc','libusb_api.cc','spi_backend.cc','msgq_adapter.cc']
  objects=[]
  for index,name in enumerate(new_sources):
   obj=a.output/(str(index)+'.o');run('compile-'+name,[a.compiler,*flags,'-MMD','-MF',a.output/(str(index)+'.d'),'-c',pkg/name,'-o',obj]);objects.append(obj)
  test=a.output/'test.o'
  run('compile-test',[a.compiler,*flags,*(['-DTEST_MSGQ_ADAPTER'] if linux else []),'-MMD','-MF',a.output/'test.d','-c',pkg/'tests/test_runtime_integration.cc','-o',test])
  old_objects=sorted(daemon.glob('[0-9][0-9].o'))
  # The old worker additionally builds separate loop/fault main objects. Retain
  # only its documented primary files, excluding its main/offline entry.
  old_objects=[q for q in old_objects if int(q.stem)<(28 if linux else 14)]
  core=[q for q in old_objects if int(q.stem) not in (7,13)]
  libs=[a.prefix/'capnproto/install/lib/libcapnp.a',a.prefix/'capnproto/install/lib/libkj.a',a.usb_library]
  if linux: libs += [a.prefix/'json11/install/lib/libjson11.a',a.prefix/'zeromq/install/lib/libzmq.a']
  extra=['-lz','-Wl,-rpath,'+str(a.usb_library.parent)]+(['-ldl','-lrt'] if linux else [])
  binary=a.output/'test_runtime_integration'
  run('link-runtime',[a.compiler,'-fsanitize=address,undefined','-pthread',*core,*objects[:5],*([objects[5]] if linux else []),test,*libs,*extra,'-o',binary])
  raw=run('runtime',[binary]);groups=[line[5:] for line in raw.splitlines() if line.startswith('PASS ')];summary=json.loads(raw.splitlines()[-1])
  if summary['passed']!=len(groups) or len(groups)!=len(set(groups)) or summary['failed'] or summary['skipped']: raise ValueError('invalid P8 case accounting')
  main_p8=None
  if linux:
   main=a.output/'actual-main.o'
   # Match the retained worker: preserve official-header warnings, while the
   # compatibility sources and their tests above keep -Werror.
   run('compile-actual-main',[a.compiler,*[f for f in flags if f!='-Werror'],'-DSONGPLUS_P8_INTEGRATION','-isystem',a.prefix/'json11/install/include','-c',a.source/'openpilot/selfdrive/pandad/main.cc','-o',main])
   full=a.output/'pandad-p8';run('link-actual-pandad-p8',[a.compiler,'-fsanitize=address,undefined','-pthread',*[q for q in old_objects if int(q.stem)!=13],main,*objects,*libs,*extra,'-o',full])
   run('actual-pandad-offline',[full,'--compat-offline'])
   main_raw=run('actual-pandad-p8-owner',[full,'--compat-runtime-offline'])
   main_p8=json.loads(main_raw.splitlines()[-1])
   if main_p8.get('passed')!=1 or main_p8.get('failed') or main_p8.get('skipped') or 'PASS p8_actual_main_unbound_owner_no_io' not in main_raw: raise ValueError('actual main owner case invalid')
   for argument in ('--compat-runtime','--compat-device'):
    run('deny-'+argument,[full,argument],timeout=10,expected=2)
  sources={str(q.relative_to(a.source)):digest(q) for q in a.source.rglob('*') if q.is_file() and '.git' not in q.parts}
  if sources!=initial_sources: raise ValueError('source modified during validation')
  report={'schema':'songplus-runtime-integration-p8-worker/v1','status':'PASS_OFFLINE_HOST_ONLY','platform':platform.platform(),'summary':summary,'groups':groups,'build_contract':build_contract,'commands':commands,'source_files_sha256':sources,'binary_sha256':digest(binary),'sanitizers':['address','undefined'],'real_device_io':False,'real_can_tx':False,'actual_pandad_relinked_with_p8':linux,'actual_main_p8_summary':main_p8,'msgq_adapter_compiled':True,'msgq_interface_executed':linux,'isolated_msgq_endpoints_opened':linux,'network_sockets_opened':False,'actual_main_p8_binary_sha256':digest(a.output/'pandad-p8') if linux else None,'retained_files_sha256':{str(q.relative_to(a.output)):digest(q) for q in a.output.rglob('*') if q.is_file() and q!=a.output/'result.json'},'native_regression':json.loads((native/'result.json').read_text()),'daemon_regression':json.loads((daemon/'result.json').read_text())}
  (a.output/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'status':report['status'],'summary':summary}))
 except Exception as exc:
  (a.output/'result.json').write_text(json.dumps({'status':'FAIL','error':str(exc),'commands':commands},indent=2)+'\n');raise
if __name__=='__main__':main()
