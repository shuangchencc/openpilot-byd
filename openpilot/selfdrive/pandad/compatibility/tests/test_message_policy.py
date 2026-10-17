"""Frozen-schema wire fixtures and selected real consumer call-site execution.

No daemon imports/startup, socket, Params, Panda constructor or native UI is used.
Actual functions/branches are AST-extracted from the patched consumer files;
only their unrelated runtime dependencies are injected. This is consumer logic
integration, not a claim that the complete daemons ran on hardware.
"""
import ast
from collections import namedtuple
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

import capnp
from compatibility.message_policy import (CAN_MAX_AGE_NS, CONTROL_MAX_AGE_NS, PANDA_MAX_AGE_NS,
                                           PERIPHERAL_MAX_AGE_NS, PROFILE_ENV, MessagePolicy,
                                           event_fresh, policy_from_environment, stream_fresh)
from compatibility.profile import load_profile
from test_profile import synthetic_dict, unbound_dict

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[3]
SCHEMA_ROOT = Path(os.environ.get('SONGPLUS_SCHEMA_ROOT', ROOT / 'openpilot/cereal'))
CAR_SCHEMA_ROOT = Path(os.environ['SONGPLUS_CAR_SCHEMA_ROOT']) if 'SONGPLUS_CAR_SCHEMA_ROOT' in os.environ else Path(files('opendbc') / 'car')
capnp.remove_import_hook()
LOG = capnp.load(str(SCHEMA_ROOT / 'log.capnp'), imports=[str(SCHEMA_ROOT), str(CAR_SCHEMA_ROOT)])
NOW = 5_000_000_000
SERIALIZED = {}
SCHEMA_HASHES = {
  'log.capnp':'60ea81bac323120e04a456901e4bbd349a377a3db53a568d6870e9371859cc10',
  'deprecated.capnp':'b3aa5c13de40001ed7376cef858500fb003f7698f96131cd0db18d878fa1af0d',
  'custom.capnp':'6b06a0d2d5ac6b902ca2f433c1663ed83ea4b41ba4c16cddfc7acd8a630be82d',
  'include/c++.capnp':'fb306076cd38c27af1aed20fac6395e9a46fbe5b5df6a248b5f1b6845a079c44',
  'car.capnp':'f39cc7be3216e3b692334b4c7dc7fc5f6509865e700b15e17acccc7b7cc644ab',
}
SELFDRIVED = 'openpilot/selfdrive/selfdrived/selfdrived.py'
HARDWARED = 'openpilot/system/hardware/hardwared.py'
CARD = 'openpilot/selfdrive/car/card.py'
UI = 'openpilot/selfdrive/ui/ui_state.py'


def wire_event(name, *, valid=True, stamp=NOW, count=2, dos_slot=0):
  event = LOG.Event.new_message(valid=valid, logMonoTime=stamp)
  if name in ('pandaStates', 'can'):
    event.init(name, count)
  else:
    event.init(name)
  if name == 'pandaStates':
    for slot, ps in enumerate(event.pandaStates):
      ps.pandaType = 'dos' if slot == dos_slot else 'redPanda'
      ps.safetyModel = 'noOutput'
      ps.harnessStatus = 'normal' if slot != dos_slot else 'notConnected'
      ps.ignitionLine = slot != dos_slot
      ps.voltage = 12000 + slot * 1000
      ps.current = 100 + slot
  elif name == 'peripheralState':
    event.peripheralState.pandaType = 'dos'
    event.peripheralState.fanSpeedRpm = 1200
  elif name == 'selfdriveState':
    event.selfdriveState.enabled = True
  elif name == 'can':
    for i, frame in enumerate(event.can):
      frame.address = 0x123 + i
      frame.dat = b'\x01\x02'
      frame.src = i * 4
  raw = event.to_bytes()
  SERIALIZED[f'{name}-{valid}-{stamp}-{count}-{dos_slot}'] = raw
  with LOG.Event.from_bytes(raw) as reader:
    return reader.as_builder()


class FakeSubMaster:
  def __init__(self, **events):
    self.events = events
    self.valid = {n: e.valid for n, e in events.items()}
    self.seen = dict.fromkeys(events, True)
    self.logMonoTime = {n: e.logMonoTime for n, e in events.items()}
    self.recv_time = dict.fromkeys(events, NOW / 1e9)
    self.updated = dict.fromkeys(events, True)
    self.alive = dict.fromkeys(events, True)
    self.frame = 2

  def __getitem__(self, name):
    return getattr(self.events[name], name)


def fixture(dos_slot=0):
  return MessagePolicy(load_profile(json.dumps(synthetic_dict(dos_slot)))), FakeSubMaster(
    pandaStates=wire_event('pandaStates', dos_slot=dos_slot), peripheralState=wire_event('peripheralState'),
    selfdriveState=wire_event('selfdriveState'), carControl=wire_event('carControl'),
    deviceState=wire_event('deviceState'), wideRoadCameraState=wire_event('wideRoadCameraState'))


def source_ast(path):
  return ast.parse((ROOT / path).read_text(), filename=path)


def actual_function(path, name, namespace, cls=None):
  tree = source_ast(path)
  scope = tree if cls is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
  node = next(n for n in scope.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
  node.decorator_list = []
  module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
  ast.fix_missing_locations(module)
  exec(compile(module, path, 'exec'), namespace)
  return namespace[name]


def hardware_branch(marker, namespace):
  tree = source_ast(HARDWARED)
  fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'hardware_thread')
  loop = next(n for n in fn.body if isinstance(n, ast.While))
  node = next(n for n in loop.body if isinstance(n, ast.If) and marker in ast.unparse(n))
  exec(compile(ast.Module(body=[node], type_ignores=[]), HARDWARED, 'exec'), namespace)


class TestMessagePolicy(unittest.TestCase):
  def test_explicit_activation_default_and_invalid_never_fallback(self):
    self.assertIsNone(policy_from_environment({}))
    for text in ('', '{', json.dumps(unbound_dict())):
      policy = policy_from_environment({PROFILE_ENV: text})
      self.assertIsInstance(policy, MessagePolicy)
      self.assertFalse(policy.bound)
    policy = policy_from_environment({PROFILE_ENV: json.dumps(synthetic_dict())})
    self.assertTrue(policy.bound)
    with self.assertRaises(TypeError):
      MessagePolicy({})

  def test_serialized_fixed_required_count_and_unknown_slots(self):
    policy, sm = fixture()
    for count in (0, 1, 2, 3):
      sm = FakeSubMaster(pandaStates=wire_event('pandaStates', count=count))
      self.assertEqual(policy.group_ready(sm, NOW), count == 2)
    sm['pandaStates'][1].pandaType = 'unknown'
    self.assertFalse(policy.group_ready(sm, NOW))
    policy, sm = fixture()
    sm['pandaStates'][0].pandaType = 'redPanda'
    self.assertFalse(policy.group_ready(sm, NOW))

  def test_wire_panda_states_matrix_consumer_reactions(self):
    policy, _ = fixture()
    for count in (0, 1, 2, 3):
      for valid in (False, True):
        for stamp in (NOW, NOW-PANDA_MAX_AGE_NS+1, NOW-PANDA_MAX_AGE_NS, NOW+1):
          sm = FakeSubMaster(pandaStates=wire_event('pandaStates', count=count, valid=valid, stamp=stamp))
          expected = count == 2 and valid and 0 <= NOW-stamp < PANDA_MAX_AGE_NS
          self.assertEqual(policy.group_ready(sm, NOW), expected)
          self.assertEqual(policy.hardware(sm, NOW).valid, expected)
          self.assertEqual(policy.hardware(sm, NOW).ignition, expected)

  def test_event_valid_seen_source_and_receive_age_boundaries(self):
    for stamp, expected in ((NOW, True), (NOW - PANDA_MAX_AGE_NS + 1, True),
                            (NOW - PANDA_MAX_AGE_NS, False), (NOW - PANDA_MAX_AGE_NS - 1, False),
                            (NOW + 1, False), (0, False)):
      policy, sm = fixture()
      sm.logMonoTime['pandaStates'] = stamp
      self.assertEqual(policy.group_ready(sm, NOW), expected)
    for flag in ('valid', 'seen'):
      policy, sm = fixture()
      getattr(sm, flag)['pandaStates'] = False
      self.assertFalse(policy.group_ready(sm, NOW))
    for received in ((NOW - PANDA_MAX_AGE_NS)/1e9, (NOW + 1)/1e9, float('nan'), float('inf'), -1):
      policy, sm = fixture()
      sm.recv_time['pandaStates'] = received
      self.assertFalse(policy.group_ready(sm, NOW))

  def test_old_source_cannot_be_refreshed_by_arrival_or_missing_update(self):
    policy, sm = fixture()
    sm.updated['pandaStates'] = False
    sm.recv_time['pandaStates'] = NOW/1e9
    sm.logMonoTime['pandaStates'] = NOW - PANDA_MAX_AGE_NS
    self.assertFalse(policy.group_ready(sm, NOW))
    self.assertFalse(policy.hardware(sm, NOW).ignition)
    self.assertIsNone(policy.hardware(sm, NOW).voltage)

  def test_both_role_permutations_ignition_harness_voltage_current(self):
    for dos_slot in (0, 1):
      policy, sm = fixture(dos_slot)
      view = policy.hardware(sm, NOW)
      self.assertTrue(view.valid and view.ignition and view.in_car)
      self.assertEqual(str(view.vehicle_type), 'redPanda')
      self.assertEqual(view.voltage, 12000 + dos_slot*1000)
      self.assertEqual(view.current, 100 + dos_slot)
      sm['pandaStates'][1-dos_slot].ignitionLine = False
      self.assertFalse(policy.hardware(sm, NOW).ignition)
      data = synthetic_dict(dos_slot)
      data['roles']['ignition_sources'] = [dos_slot]
      policy = MessagePolicy(load_profile(json.dumps(data)))
      sm['pandaStates'][1-dos_slot].ignitionLine = True
      self.assertFalse(policy.hardware(sm, NOW).ignition)

  def test_fan_role_capability_validity_freshness_and_missing_board(self):
    for dos_slot in (0, 1):
      policy, sm = fixture(dos_slot)
      self.assertEqual(policy.fan_rpm(sm, NOW), 1200)
      sm.logMonoTime['peripheralState'] = NOW-PERIPHERAL_MAX_AGE_NS+1
      self.assertEqual(policy.fan_rpm(sm, NOW), 1200)
      sm.logMonoTime['peripheralState'] -= 1
      self.assertIsNone(policy.fan_rpm(sm, NOW))
      policy, sm = fixture(dos_slot)
      sm.valid['peripheralState'] = False
      self.assertIsNone(policy.fan_rpm(sm, NOW))
      sm.valid['peripheralState'] = True
      sm['peripheralState'].pandaType = 'redPanda'
      self.assertIsNone(policy.fan_rpm(sm, NOW))
      policy, sm = fixture(dos_slot)
      sm.valid['pandaStates'] = False
      self.assertIsNone(policy.fan_rpm(sm, NOW))
    data = synthetic_dict()
    data['slots'][0]['capabilities']['fan'] = False
    data['slots'][0]['capabilities']['power_monitor'] = False
    data['roles']['fan_control'] = data['roles']['power_monitor'] = 'UNSUPPORTED'
    policy = MessagePolicy(load_profile(json.dumps(data)))
    _, sm = fixture()
    self.assertIsNone(policy.fan_rpm(sm, NOW))
    self.assertIsNone(policy.hardware(sm, NOW).voltage)

  def test_serialized_enabled_and_control_states_never_authorize_profile(self):
    policy, sm = fixture()
    for valid, stamp in ((True, NOW), (False, NOW), (True, NOW-CONTROL_MAX_AGE_NS), (True, NOW+1)):
      for name in ('selfdriveState', 'carControl'):
        sm.events[name] = wire_event(name, valid=valid, stamp=stamp)
        sm.valid[name], sm.logMonoTime[name] = valid, stamp
      self.assertFalse(policy.enabled(sm, NOW))
      self.assertFalse(policy.controls_permitted(sm, NOW))

  def test_can_event_validity_age_flags_and_reserved_bus(self):
    policy, _ = fixture()
    for valid, stamp, expected in ((True,NOW,True), (False,NOW,False),
                                    (True,NOW-CAN_MAX_AGE_NS+1,True), (True,NOW-CAN_MAX_AGE_NS,False), (True,NOW+1,False)):
      self.assertEqual(policy.accept_can(wire_event('can',valid=valid,stamp=stamp),NOW),expected)
    for bus in (0,1,2,4,5,6,128,129,130,132,133,134,192,193,194,196,197,198,3,7,64,255):
      event = wire_event('can',count=1)
      event.can[0].src = bus
      self.assertEqual(policy.accept_can(event,NOW),bus not in (3,7,64,255))
    self.assertFalse(event_fresh(wire_event('selfdriveState'),'can',NOW,CAN_MAX_AGE_NS))
    self.assertFalse(event_fresh(object(),'can',NOW,CAN_MAX_AGE_NS))
    self.assertFalse(stream_fresh(object(),'can',NOW,CAN_MAX_AGE_NS))

  def test_actual_card_callbacks_filter_serialized_events_and_block_queries(self):
    policy,_ = fixture()
    events = [wire_event('can'),wire_event('can',valid=False),wire_event('can',stamp=NOW-CAN_MAX_AGE_NS)]
    sender = Mock()
    ns = {'messaging':types.SimpleNamespace(drain_sock=lambda *a,**k:events),
          'CanData':namedtuple('CanData','address dat src'), 'time':types.SimpleNamespace(monotonic_ns=lambda:NOW),
          'can_list_to_can_capnp':lambda *a,**kw:b'wire', 'cloudlog':Mock()}
    callbacks = actual_function(CARD,'can_comm_callbacks',ns)
    receive,send = callbacks(None,sender,policy)
    self.assertEqual(len(receive()),1)
    self.assertEqual(receive()[0][1].src,4)
    send([])
    sender.send.assert_not_called()
    default_receive,default_send = callbacks(None,sender)
    self.assertEqual(len(default_receive()),3)
    default_send([])
    sender.send.assert_called_once_with(b'wire')
    obd = actual_function(CARD,'obd_callback',ns)
    params = Mock()
    with self.assertRaisesRegex(RuntimeError,'disables'):
      obd(params,policy)(True)
    self.assertEqual(params.mock_calls,[])

  def test_actual_card_constructor_and_control_gate_before_side_effects(self):
    policy,_ = fixture()
    ns = {'policy_from_environment':lambda:policy}
    constructor = actual_function(CARD,'__init__',ns,'Car')
    with self.assertRaisesRegex(RuntimeError,'disables'):
      constructor(types.SimpleNamespace())
    update = actual_function(CARD,'controls_update',{},'Car')
    car = types.SimpleNamespace(panda_policy=policy)
    update(car,None,None)  # would fail on any CI/Params/sendcan access
    car.panda_policy=None
    car.initialized_prev=True
    car.sm=types.SimpleNamespace(all_alive=lambda _:False)
    update(car,None,None)

  def test_actual_selfdrived_gate_restart_and_reconnect_remain_disabled(self):
    policy,sm = fixture()
    disabled = LOG.SelfdriveState.OpenpilotState.disabled
    gate = actual_function(SELFDRIVED,'apply_panda_gate',{'State':LOG.SelfdriveState.OpenpilotState},'SelfdriveD')
    for p in (policy,policy_from_environment({PROFILE_ENV:'bad'})):
      obj=types.SimpleNamespace(panda_policy=p,sm=sm,enabled=True,active=True,
                                state_machine=types.SimpleNamespace(state=LOG.SelfdriveState.OpenpilotState.enabled))
      gate(obj)
      self.assertFalse(obj.enabled or obj.active)
      self.assertEqual(obj.state_machine.state,disabled)
      sm.valid['pandaStates']=False
      gate(obj)
      sm.valid['pandaStates']=True
      gate(obj)
      self.assertFalse(obj.enabled)
    obj.panda_policy=None
    obj.enabled=obj.active=True
    gate(obj)
    self.assertTrue(obj.enabled and obj.active)

  def test_actual_selfdrived_serialized_publication_marks_invalid_group(self):
    policy,sm=fixture()
    out=[]
    def new_message(name,*args):
      event=LOG.Event.new_message(logMonoTime=NOW)
      event.init(name,*args)
      return event
    class Events:
      names=[]
      def contains(self,_): return False
    alert=types.SimpleNamespace(alert_text_1='',alert_text_2='',alert_size='none',alert_status='normal',
                                alert_type='',audible_alert='none',visual_alert='none')
    ns={'messaging':types.SimpleNamespace(new_message=new_message),'time':types.SimpleNamespace(monotonic_ns=lambda:NOW),
        'ET':types.SimpleNamespace(NO_ENTRY=1),'DT_CTRL':0.01,'State':LOG.SelfdriveState.OpenpilotState}
    publish=actual_function(SELFDRIVED,'publish_selfdriveState',ns,'SelfdriveD')
    obj=types.SimpleNamespace(panda_policy=policy,sm=sm,enabled=True,active=True,events=Events(),events_prev=[],
                             state_machine=types.SimpleNamespace(state='disabled'),personality='standard',
                             AM=types.SimpleNamespace(current_alert=alert),pm=types.SimpleNamespace(send=lambda n,e:out.append(e.to_bytes())))
    for valid in (True,False):
      sm.valid['pandaStates']=valid
      publish(obj,None)
      with LOG.Event.from_bytes(out[-1]) as e:
        self.assertEqual(e.valid,valid)
        self.assertFalse(e.selfdriveState.enabled or e.selfdriveState.active or e.selfdriveState.engageable)
        self.assertEqual(e.selfdriveState.state,LOG.SelfdriveState.OpenpilotState.disabled)
    obj.panda_policy=None
    publish(obj,None)
    with LOG.Event.from_bytes(out[-1]) as e:
      self.assertTrue(e.valid and e.selfdriveState.enabled)

  def test_actual_hardware_ignition_harness_role_and_frozen_event_exit(self):
    for dos_slot in (0,1):
      policy,sm=fixture(dos_slot)
      ns={'panda_policy':policy,'sm':sm,'pandaStates':sm['pandaStates'],
          'onroad_conditions':{'ignition':True}, 'time':types.SimpleNamespace(monotonic_ns=lambda:NOW),
          'log':LOG}
      hardware_branch('panda_observation =',ns)
      self.assertTrue(ns['onroad_conditions']['ignition'] and ns['in_car'])
      sm.valid['pandaStates']=False
      sm.updated['pandaStates']=False
      hardware_branch('panda_observation =',ns)
      self.assertFalse(ns['onroad_conditions']['ignition'] or ns['in_car'])
      ns['panda_policy']=None
      sm.updated['pandaStates']=True
      hardware_branch('panda_observation =',ns)
      self.assertEqual(ns['in_car'],dos_slot!=0)  # default upstream index zero unchanged

  def test_actual_hardware_invalid_or_frozen_enabled_clears_is_engaged(self):
    policy,sm=fixture()
    sm.updated['selfdriveState']=False
    params=Mock()
    ns={'panda_policy':policy,'sm':sm,'time':types.SimpleNamespace(monotonic_ns=lambda:NOW),
        'engaged_prev':True,'params':params,'open':Mock(side_effect=OSError('test device write disabled'))}
    hardware_branch("sm.updated['selfdriveState']",ns)
    params.put_bool.assert_called_once_with('IsEngaged',False,block=True)
    self.assertFalse(ns['engaged_prev'])
    ns['panda_policy']=None
    params.reset_mock()
    hardware_branch("sm.updated['selfdriveState']",ns)
    params.put_bool.assert_not_called()

  def test_actual_ui_role_validity_and_stale_engagement(self):
    policy,sm=fixture(1)
    sm['deviceState'].started=True
    sm.updated['wideRoadCameraState']=False
    ns={'time':types.SimpleNamespace(monotonic_ns=lambda:NOW),'log':LOG}
    state=actual_function(UI,'_update_state',ns,'UIState')
    engaged=actual_function(UI,'engaged',ns,'UIState')
    obj=types.SimpleNamespace(panda_policy=policy,sm=sm,CP=None,is_body=False,light_sensor=0.)
    state(obj)
    self.assertTrue(obj.ignition and obj.started)
    self.assertEqual(str(obj.panda_type),'redPanda')
    self.assertFalse(engaged(obj))
    sm.valid['pandaStates']=False
    state(obj)
    self.assertFalse(obj.ignition or obj.started)
    self.assertEqual(obj.panda_type,LOG.PandaState.PandaType.unknown)
    obj.panda_policy=None
    obj.started=True
    self.assertTrue(engaged(obj))

  def test_actual_consumer_hook_locations_and_schema_unchanged_contract(self):
    for path in (SELFDRIVED,HARDWARED,CARD,UI):
      text=(ROOT/path).read_text()
      self.assertIn('policy_from_environment()',text)
      compile(text,path,'exec')
    step=next(n for n in ast.walk(source_ast(SELFDRIVED)) if isinstance(n,ast.FunctionDef) and n.name=='step')
    self.assertIn('self.apply_panda_gate()',ast.unparse(step))
    text=(ROOT/SELFDRIVED).read_text()
    self.assertIn("self.panda_policy is None and REPLAY",text)
    self.assertNotIn('SONGPLUS', (SCHEMA_ROOT/'log.capnp').read_text())
    for name, expected in SCHEMA_HASHES.items():
      path = (CAR_SCHEMA_ROOT if name == 'car.capnp' else SCHEMA_ROOT) / name
      self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, name)

  @classmethod
  def tearDownClass(cls):
    dest=os.environ.get('SONGPLUS_CONSUMER_ARTIFACT_DIR')
    if dest:
      directory=Path(dest)
      directory.mkdir(parents=True,exist_ok=True)
      manifest=[]
      for i,(name,raw) in enumerate(sorted(SERIALIZED.items())):
        filename=f'{i:03d}.capnp'
        (directory/filename).write_bytes(raw)
        manifest.append({'fixture':name,'file':filename,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
      (directory/'manifest.json').write_text(json.dumps({
        'schema':'songplus-p4-cereal-fixtures/v1', 'fixtures':manifest, 'schema_sha256':SCHEMA_HASHES,
        'consumer_sha256':{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
                           for path in (SELFDRIVED,HARDWARED,CARD,UI)},
        'message_policy_sha256':hashlib.sha256((PACKAGE/'message_policy.py').read_bytes()).hexdigest(),
        'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'execution_scope':'selected actual consumer functions/branches with injected dependencies; no daemon startup',
      },indent=2)+'\n')


if __name__=='__main__':
  unittest.main(verbosity=2)
