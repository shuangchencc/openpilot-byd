"""Driver monitoring on hardware built without a cabin camera.

Some devices ship with no cabin (driver) camera at all, so dmonitoringmodeld cannot run and
driverStateV2 is never published. dmonitoringd keeps running: with no face to see, policy.py
selects its wheeltouch policy, which is the same path upstream takes on a camera-equipped car
whose camera cannot make out a face. test_monitoring.py already covers that path.

What is specific here - and the reason dmonitoringd synthesizes a driverStateV2 rather than
letting the default one through - is the trap in the first test below.
"""
from openpilot.common.test import OpenpilotTestCase
from openpilot.cereal import log
from openpilot.common.realtime import DT_DMON
from openpilot.selfdrive.monitoring.dmonitoringd import (no_face_driver_state,
                                                         SubMasterNoCabinCamera,
                                                         HandsOnTorque,
                                                         HANDS_ON_TORQUE,
                                                         NO_CABIN_CAMERA_IGNORE,
                                                         CALIBRATION_VALIDITY_NOT_REQUIRED)
from openpilot.selfdrive.monitoring.policy import DriverMonitoring

MonitoringPolicy = log.DriverMonitoringState.MonitoringPolicy

TEST_TIMESPAN = 120  # seconds
N = int(TEST_TIMESPAN / DT_DMON)


def _run(msg, interaction):
  DM = DriverMonitoring()
  alert_lvls = []
  for idx in range(N):
    DM._update_states(msg, [0, 0, 0], 0, True, False)
    DM._update_events(interaction[idx], True, False, 0)
    alert_lvls.append(DM.alert_level)
  return alert_lvls, DM


NEVER_TOUCHES = [False] * N
# a touch every 5 s, well inside the 25 s wheeltouch timeout
TOUCHES_EVERY_5S = [(i % int(5 / DT_DMON)) == 0 for i in range(N)]


class TestNoCabinCamera(OpenpilotTestCase):
  def test_default_message_would_strand_the_driver(self):
    # Why dmonitoringd must not just pass the default driverStateV2 through. Its
    # faceOrientation / facePosition lists are zero-length, so _update_states returns early
    # (policy.py "if not all(len(x) > 0 ...)") - before the line that picks the policy. The
    # policy therefore stays on its initial value, vision, and the wheel-touch reset in
    # _update_events is gated on the wheeltouch policy: the driver would escalate to a red
    # alert and a lockout that touching the wheel cannot clear.
    default = log.DriverStateV2.new_message()
    _, DM = _run(default, TOUCHES_EVERY_5S)
    assert DM.active_policy == MonitoringPolicy.vision, "if this ever becomes wheeltouch, the synthesis is no longer needed"
    assert DM.lockout_active, "the trap: locked out despite touching the wheel every 5 s"

  def test_synthesized_state_selects_wheeltouch(self):
    _, DM = _run(no_face_driver_state(), NEVER_TOUCHES)
    assert not DM.face_detected
    assert DM.active_policy == MonitoringPolicy.wheeltouch

  def test_escalates_and_locks_out_when_the_wheel_is_never_touched(self):
    alert_lvls, DM = _run(no_face_driver_state(), NEVER_TOUCHES)
    s = DM.settings
    assert alert_lvls[int(s._WHEELTOUCH_POLICY_ALERT_1_TIMEOUT / 2 / DT_DMON)] == 0
    assert alert_lvls[int((s._WHEELTOUCH_POLICY_ALERT_1_TIMEOUT +
                           (s._WHEELTOUCH_POLICY_ALERT_2_TIMEOUT - s._WHEELTOUCH_POLICY_ALERT_1_TIMEOUT) / 2) / DT_DMON)] == 1
    assert alert_lvls[int((s._WHEELTOUCH_POLICY_ALERT_2_TIMEOUT +
                           (s._WHEELTOUCH_POLICY_ALERT_3_TIMEOUT - s._WHEELTOUCH_POLICY_ALERT_2_TIMEOUT) / 2) / DT_DMON)] == 2
    assert alert_lvls[-1] == 3
    assert DM.lockout_active, "an untouched wheel must still reach lockout with no camera"

  def test_wheel_touches_keep_it_clear(self):
    # carState.steeringPressed is what feeds this on a real car; on the Song PLUS it comes
    # from DRIVER_TORQUE > 24 Nm, which reproduces the logged steeringPressed at 100.000%.
    alert_lvls, DM = _run(no_face_driver_state(), TOUCHES_EVERY_5S)
    assert DM.active_policy == MonitoringPolicy.wheeltouch
    assert not DM.lockout_active
    assert max(alert_lvls) == 0, f"regular wheel touches should never alert, peaked at {max(alert_lvls)}"


class TestNoCabinCameraWiring(OpenpilotTestCase):
  """The policy tests above pass a synthesized driverStateV2 straight to DriverMonitoring,
  so they cannot see whether dmonitoringd ever calls run_step(). It did not: the first
  version named driverStateV2 in ignore_alive and ignore_valid but not ignore_avg_freq, and
  all_checks() is the AND of all three (cereal/messaging:277). all_checks() stayed False,
  `elif valid: DM.run_step(...)` never fired, and activePolicy sat at `vision` while
  driverMonitoringState still published at 20 Hz - invisible from outside, found only by
  reading activePolicy on the device. These tests cover the wiring the others cannot.
  """

  def test_every_submaster_ignore_list_covers_the_missing_service(self):
    """Fails if SubMaster grows another ignore_* knob that dmonitoringd does not pass.

    Deliberately derived from SubMaster's own signature rather than a hardcoded list of
    three, so a fourth check added upstream shows up here instead of silently disabling
    driver monitoring again.
    """
    import ast
    import inspect
    import openpilot.cereal.messaging as messaging
    from openpilot.selfdrive.monitoring import dmonitoringd

    knobs = {n for n in inspect.signature(messaging.SubMaster.__init__).parameters
             if n.startswith('ignore_')}
    assert knobs, "SubMaster has no ignore_* parameters; this test needs rewriting"

    src = inspect.getsource(dmonitoringd)
    tree = ast.parse(src)
    passed = {}
    for node in ast.walk(tree):
      if not isinstance(node, ast.Call):
        continue
      f = node.func
      name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
      if name != 'SubMaster':
        continue
      for kw in node.keywords:
        if kw.arg and kw.arg.startswith('ignore_'):
          passed[kw.arg] = ast.unparse(kw.value)

    missing = knobs - set(passed)
    assert not missing, (
      f"dmonitoringd does not pass {sorted(missing)} to SubMaster. Every ignore_* list must "
      "name the missing service, because all_checks() ANDs all of them together; omitting "
      "one leaves all_checks() False forever and run_step() is never called."
    )
    for knob, value in sorted(passed.items()):
      assert 'NO_CABIN_CAMERA_IGNORE' in value, (
        f"{knob}={value} must include the shared NO_CABIN_CAMERA_IGNORE constant so the "
        "lists cannot drift apart. Adding another service is fine (ignore_valid also "
        "carries CALIBRATION_VALIDITY_NOT_REQUIRED), but the synthesized service has to "
        "be in every one of them."
      )

  def test_the_ignored_service_is_the_one_we_synthesize(self):
    """The service we tell SubMaster to ignore must be the one we substitute."""
    assert NO_CABIN_CAMERA_IGNORE == ['driverStateV2']
    synthesized = no_face_driver_state()
    # The proxy hands back the synthesized message for exactly the ignored service and
    # passes everything else through to the real SubMaster.
    proxy = SubMasterNoCabinCamera({'carState': 'from the real SubMaster'}, synthesized)
    for service in NO_CABIN_CAMERA_IGNORE:
      assert proxy[service] is synthesized
    assert proxy['carState'] == 'from the real SubMaster'
    # And what it hands back is a valid payload for that Event member: pycapnp refuses to
    # assign a struct of any other type. (no_face_driver_state() returns the bare struct,
    # not an Event, so it has no union to call which() on.)
    evt = log.Event.new_message()
    evt.driverStateV2 = synthesized
    assert evt.which() == 'driverStateV2'

  def test_calibration_validity_is_relaxed_but_liveness_is_not(self):
    """Only ignore_valid may carry the calibration service - never alive or frequency.

    extrinsicsCalibration does arrive, at 4 Hz. Relaxing its validity lets wheel-touch
    monitoring work before the car has been driven; relaxing its liveness would also hide
    calibrationd dying, which is a real fault we still want all_checks() to catch.
    """
    import ast
    import inspect
    from openpilot.selfdrive.monitoring import dmonitoringd

    tree = ast.parse(inspect.getsource(dmonitoringd))
    passed = {}
    for node in ast.walk(tree):
      if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
        if name == 'SubMaster':
          for kw in node.keywords:
            if kw.arg and kw.arg.startswith('ignore_'):
              passed[kw.arg] = ast.unparse(kw.value)

    const = 'CALIBRATION_VALIDITY_NOT_REQUIRED'
    assert const in passed.get('ignore_valid', ''), \
      f"ignore_valid should carry {const}; got {passed.get('ignore_valid')!r}"
    for knob in ('ignore_alive', 'ignore_avg_freq'):
      assert const not in passed.get(knob, ''), (
        f"{knob} must NOT carry {const}: extrinsicsCalibration really does arrive, and "
        "hiding its absence would hide calibrationd failing"
      )
    assert CALIBRATION_VALIDITY_NOT_REQUIRED == ['extrinsicsCalibration']

  def test_submaster_is_told_the_loop_rate(self):
    """Replacing poll= with a Ratekeeper means frequency= has to be supplied explicitly.

    SubMaster sizes each service's acceptable rate window against update_freq, which is
    `frequency or max(freq of polled services)` (cereal/messaging:216). With neither poll=
    nor frequency=, that max is 100 Hz (carState), and a 20 Hz loop can never satisfy the
    resulting 40..120 Hz window for carState because update() takes one message per socket
    per call. all_checks() then stays False forever and driver monitoring silently never
    runs.
    """
    import ast
    import inspect
    from openpilot.selfdrive.monitoring import dmonitoringd

    tree = ast.parse(inspect.getsource(dmonitoringd))
    calls = []
    for node in ast.walk(tree):
      if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
        if name == 'SubMaster':
          calls.append({kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg})

    assert calls, "no SubMaster construction found"
    for kwargs in calls:
      if 'poll' in kwargs:
        continue   # upstream path: update_freq comes from the polled service
      assert 'frequency' in kwargs, (
        "a SubMaster built without poll= must be given frequency=, or its rate windows are "
        f"sized against the fastest subscription instead of the loop. got {kwargs}"
      )
      assert 'DT_DMON' in kwargs['frequency'], (
        f"frequency should be derived from DT_DMON so it cannot drift from the Ratekeeper; "
        f"got {kwargs['frequency']}"
      )


class FakeCarState:
  def __init__(self, steering_torque, steering_pressed=False):
    self.steeringTorque = steering_torque
    self.steeringPressed = steering_pressed
    self.gasPressed = False
    self.vEgo = 8.3


class FakeSubMaster:
  """Stands in for messaging.SubMaster: update() moves to the next carState in a script."""
  def __init__(self, torques, pressed=False):
    self._script = list(torques)
    self._pressed = pressed
    self.cs = None
    self.updates = 0

  def update(self, timeout=100):
    self.cs = FakeCarState(self._script[min(self.updates, len(self._script) - 1)], self._pressed)
    self.updates += 1

  def __getitem__(self, service):
    assert service == 'carState'
    return self.cs


class TestHandsOnTorque(OpenpilotTestCase):
  """Driver monitoring counts a light hold on the wheel as a touch, without lowering steeringPressed.

  Numbers are openpilot-a0's labelled capture of 2026-11-13 (route 00000013): untouched -1.33,
  natural two-hand hold mean +2.64, one hand mean +3.41, driving zero -1.1.
  """

  def _ticks(self, torques):
    det = HandsOnTorque.for_car("BYD_SONG_PLUS_DMI_23")
    return [det.update(t) for t in torques]

  def test_only_the_song_plus_has_values(self):
    assert HandsOnTorque.for_car("BYD_SONG_PLUS_DMI_23") is not None
    assert HandsOnTorque.for_car("BYD_ATTO_3") is None
    assert HandsOnTorque.for_car("TOYOTA_RAV4") is None
    assert set(HANDS_ON_TORQUE) == {"BYD_SONG_PLUS_DMI_23"}

  def test_untouched_wheel_never_counts(self):
    # standstill untouched readings, plus the -3.2 residue after a turn (11/12) is still under
    assert not any(self._ticks([-1.33, -1.27, -1.39, -1.1, -3.2, -1.33] * 20))

  def test_natural_hold_counts_after_two_ticks(self):
    assert self._ticks([2.64, 2.64]) == [False, True]
    assert self._ticks([3.41, 3.41, 3.41]) == [False, True, True]
    # either direction, measured from the zero, not from 0
    assert self._ticks([-4.2, -4.2]) == [False, True]     # |-4.2 + 1.1| = 3.1
    assert self._ticks([-3.9, -3.9]) == [False, False]    # |-3.9 + 1.1| = 2.8

  def test_a_single_bump_does_not_count(self):
    assert not any(self._ticks([-1.3, 4.0, -1.3, 4.0, -1.3]))

  def test_proxy_widens_steering_pressed_for_monitoring_only(self):
    det = HandsOnTorque.for_car("BYD_SONG_PLUS_DMI_23")
    real = FakeSubMaster([2.64, 2.64, 2.64])
    sm = SubMasterNoCabinCamera(real, no_face_driver_state(), det)
    sm.update(0)
    assert sm['carState'].steeringPressed is False         # one tick is not enough
    sm.update(0)
    seen = sm['carState']
    assert seen.steeringPressed is True                    # what driver monitoring sees
    assert seen.vEgo == 8.3 and seen.gasPressed is False   # everything else passes through
    assert real['carState'].steeringPressed is False        # the real carState is untouched
    # several reads in one tick must not advance the detector
    for _ in range(5):
      _ = sm['carState']
    assert det.ticks == 2

  def test_without_a_detector_carstate_is_the_real_one(self):
    real = FakeSubMaster([9.9])
    sm = SubMasterNoCabinCamera(real, no_face_driver_state())
    sm.update(0)
    assert sm['carState'] is real.cs

  def test_dmonitoringd_passes_the_detector_and_keeps_steering_pressed_at_24(self):
    import ast
    import inspect
    from openpilot.selfdrive.monitoring import dmonitoringd
    from opendbc.car.byd.values import CarControllerParams
    tree = ast.parse(inspect.getsource(dmonitoringd.dmonitoringd_thread))
    proxies = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and getattr(n.func, 'id', None) == 'SubMasterNoCabinCamera']
    assert proxies and all(len(n.args) == 3 for n in proxies), "the hands-on detector is not passed in"
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert 'HandsOnTorque.for_car' in calls
    # The decoupling: the port's steeringPressed stays a deliberate-override signal, because it
    # also drives the lateral override state and the lane-change nudge (and, planned, dropping
    # STEER_REQ while the driver overrides). Lowering it is a separate decision.
    assert CarControllerParams.STEER_DRIVER_OVERRIDE_NM == 24.0

