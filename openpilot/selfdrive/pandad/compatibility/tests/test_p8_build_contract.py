#!/usr/bin/env python3
"""Execute the actual SConscript with inert SCons/libusb bindings."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

source = Path(__file__).resolve().parents[2] / 'SConscript'

class Env:
  def __init__(self, calls, clone=False):
    self.calls = calls
    self.clone = clone
    self.options = {}
  def Clone(self):
    return Env(self.calls, True)
  def Append(self, **options):
    self.options.update(options)
  def Library(self, name, sources):
    self.calls.append(('library', name, sources, self.clone, dict(self.options), None))
    return 'panda-library'
  def Program(self, name, sources, LIBS):
    self.calls.append(('program', name, sources, self.clone, dict(self.options), LIBS))

cases = []
sys.modules['libusb'] = SimpleNamespace(INCLUDE_DIR='pinned/include', LIB_DIR='pinned/lib')
for platform in ('Darwin', 'Linux'):
  calls = []
  scope = dict(Import=lambda *args: None, GetOption=lambda name: True,
               env=Env(calls), arch=platform, common='common', messaging='messaging')
  exec(compile(source.read_text(), str(source), 'exec'), scope)
  if platform == 'Darwin':
    assert calls == []
    case = 'p8_scons_darwin_preserves_original_exclusion'
  else:
    library, daemon, extras = calls
    assert library[:3] == ('library', 'panda', ['panda.cc', 'spi.cc'])
    assert daemon[1] == 'pandad' and daemon[3]
    assert daemon[4]['LIBPATH'] == ['pinned/lib']
    assert daemon[4]['CPPPATH'] == ['pinned/include']
    assert daemon[4]['CPPDEFINES'] == ['SONGPLUS_P8_INTEGRATION', 'SONGPLUS_P10_INTEGRATION']
    assert daemon[5] == ['panda-library', 'common', 'messaging', 'pthread', 'usb-1.0']
    assert 'compatibility/runtime_integration.cc' in daemon[2]
    assert 'compatibility/msgq_adapter.cc' in daemon[2]
    for name in ('production_gate', 'discovery_scheduler', 'spi_freshness', 'native_discovery_executor'):
      assert 'compatibility/' + name + '.cc' in daemon[2]
    assert extras[1] == 'tests/test_pandad_canprotocol' and not extras[3]
    assert extras[5] == ['panda-library', 'common', 'messaging', 'pthread']
    case = 'p8_scons_daemon_pinned_usb_does_not_leak_to_extras'
  cases.append(case)
  print('PASS ' + case)
print(json.dumps(dict(suite='p8_build_contract', passed=len(cases), failed=0,
                      skipped=0, groups=cases, actual_scons_build=False)))
