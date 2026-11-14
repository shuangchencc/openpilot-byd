"""Driver monitoring on hardware built without a cabin camera.

Some devices ship with no cabin (driver) camera at all, so dmonitoringmodeld cannot run and
driverStateV2 is never published. dmonitoringd keeps running: with no face to see, policy.py
selects its wheeltouch policy, which is the same path upstream takes on a camera-equipped car
whose camera cannot make out a face. test_monitoring.py already covers that path.

What is specific here - and the reason dmonitoringd synthesizes a driverStateV2 rather than
letting the default one through - is the trap in the first test below.
"""
import math
import random

from openpilot.common.test import OpenpilotTestCase
from openpilot.cereal import log
from openpilot.common.realtime import DT_DMON
from openpilot.selfdrive.monitoring.dmonitoringd import (no_face_driver_state,
                                                         SubMasterNoCabinCamera,
                                                         HandsOnTorque,
                                                         HANDS_ON_TORQUE,
                                                         TORQUE_PER_INERTIA_ACCEL,
                                                         SAMPLE_DT, SMOOTH_N, CHAIN_N, ACCEL_DELAY, GAP_S, WINDOW_N,
                                                         centred_accel,
                                                         carstate_feed,
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


SONG_PLUS = "BYD_SONG_PLUS_DMI_23"
ZERO = HANDS_ON_TORQUE[SONG_PLUS]["zero"]
INERTIA = HANDS_ON_TORQUE[SONG_PLUS]["inertia"]


def song_plus():
  return HandsOnTorque.for_car(SONG_PLUS)


def feed_ticks(det, torques, angles=None, t0=0.0):
  """Feed 100 Hz carStates (torque, angle) and ask once per DM tick (every 5th carState)."""
  if angles is None:
    angles = [0.0] * len(torques)
  out = []
  for n, (tq, ang) in enumerate(zip(torques, angles, strict=True)):
    det.feed(t0 + n * SAMPLE_DT, tq, ang)
    if n % 5 == 4:
      out.append(det.tick())
  return out


def resting_hand(level, wobble, n, hz=2.0):
  """A hand on the wheel: a mean torque with a small wobble, on the sensor's 0.1 grid."""
  return [round(level + wobble * math.sin(2 * math.pi * hz * i * SAMPLE_DT), 1) for i in range(n)]


def eps_turning(amplitude_deg, hz, n):
  """The EPS swinging the wheel, hands off: the angle on the sensor's 0.1 deg grid and the torque
  the sensor reads from the wheel's own inertia alone (on its 0.1 grid)."""
  w = 2 * math.pi * hz
  angles = [round(amplitude_deg * math.sin(w * i * SAMPLE_DT), 1) for i in range(n)]
  accel = [-amplitude_deg * w * w * math.sin(w * i * SAMPLE_DT) for i in range(n)]
  torques = [round(ZERO - TORQUE_PER_INERTIA_ACCEL * INERTIA * a, 1) for a in accel]
  return torques, angles, max(abs(a) for a in accel)


class TestHandsOnTorque(OpenpilotTestCase):
  """Driver monitoring counts a hand resting on the wheel as a touch, without lowering steeringPressed.

  The rule (dmonitoringd.HANDS_ON_TORQUE): c = torque - zero + K * inertia * thetaddot; with the
  wheel quiet (max |thetaddot| over 1 s < 100 deg/s^2) a touch is a 1 s peak-to-peak of c of at
  least 1.5, otherwise |c| > 3.0; held for 2 DM ticks. Levels are openpilot-a0's labelled capture
  of 2026-11-13 (route 00000013): untouched -1.33, natural two-hand hold mean +2.64, driving zero -1.1.
  """

  def test_only_the_song_plus_has_values(self):
    assert song_plus() is not None
    assert HandsOnTorque.for_car("BYD_ATTO_3") is None
    assert HandsOnTorque.for_car("TOYOTA_RAV4") is None
    assert set(HANDS_ON_TORQUE) == {SONG_PLUS}

  def test_thetaddot_is_the_offline_chain(self):
    # the online detector must compute what migration-audit/closed-course-20261114 evaluated
    import numpy as np
    rng = np.random.default_rng(1)
    ang = np.round(np.cumsum(rng.normal(0, 0.3, 300)), 1)
    k = np.ones(SMOOTH_N) / SMOOTH_N
    ref = np.gradient(np.convolve(np.gradient(np.convolve(ang, k, mode="same"), SAMPLE_DT), k, mode="same"), SAMPLE_DT)
    for n in range(30, 290):
      assert abs(centred_accel(list(ang[n - CHAIN_N + 1:n + 1])) - ref[n - ACCEL_DELAY]) < 1e-6
    # and it is exact on a parabola: 200 deg/s^2
    assert abs(centred_accel([100. * (i * SAMPLE_DT) ** 2 for i in range(CHAIN_N)]) - 200.) < 1e-6

  def test_the_step_is_the_carstate_period_not_the_timestamps(self):
    # carState timestamps jitter by several ms (pandad batches); the same angles must give the
    # same result however the timestamps fall
    torques, angles, _ = eps_turning(10., 0.5, 300)
    jittered = [n * SAMPLE_DT + (0.004 if n % 3 == 0 else -0.003 if n % 3 == 1 else 0.) for n in range(300)]
    a, b = song_plus(), song_plus()
    for n in range(300):
      a.feed(n * SAMPLE_DT, torques[n], angles[n])
      b.feed(jittered[n], torques[n], angles[n])
    assert list(a._window) == list(b._window)

  def test_torque_is_paired_with_the_thetaddot_of_the_same_carstate(self):
    # thetaddot of carState n-6 is final once carState n is in; the torque of n-6 goes with it
    det = song_plus()
    torques = [0.1 * i for i in range(40)]                       # a different torque in every carState
    angles = [100. * (i * SAMPLE_DT) ** 2 for i in range(40)]    # 200 deg/s^2 throughout
    for i in range(40):
      det.feed(i * SAMPLE_DT, torques[i], angles[i])
    c = [w[0] for w in det._window]
    expected = [torques[n - ACCEL_DELAY] - ZERO + TORQUE_PER_INERTIA_ACCEL * INERTIA * 200. for n in range(CHAIN_N - 1, 40)]
    assert len(c) == len(expected) and all(abs(a - b) < 1e-6 for a, b in zip(c, expected, strict=True))
    assert all(abs(w[1] - 200.) < 1e-6 for w in det._window)

  def test_activity_is_remembered_for_one_second(self):
    # the peak-to-peak looks back WINDOW_N carStates: a hand that has let go still counts until
    # its last swing has left the window, at most a second later, then no longer
    burst = resting_hand(2.64, 0.8, 100) + [2.6] * 300           # 1 s of a hand, then a steady offset
    ticks = feed_ticks(song_plus(), burst)
    last_touch = max(i for i, x in enumerate(ticks) if x) * 5 * SAMPLE_DT
    assert 1.0 + 0.5 <= last_touch <= 1.0 + (WINDOW_N + ACCEL_DELAY) * SAMPLE_DT + 0.1, last_touch
    assert not any(ticks[int(2.2 / (5 * SAMPLE_DT)):])

  def test_an_untouched_still_wheel_never_counts(self):
    rng = random.Random(0)
    sensor_noise = [round(ZERO + rng.choice((-0.1, 0., 0., 0.1)), 1) for _ in range(1000)]
    assert not any(feed_ticks(song_plus(), sensor_noise))
    assert not any(feed_ticks(song_plus(), [-1.33] * 1000))      # standstill zero
    assert not any(feed_ticks(song_plus(), [-3.2] * 1000))       # residue after a turn (11/12), held

  def test_a_resting_hand_counts(self):
    ticks = feed_ticks(song_plus(), resting_hand(2.64, 0.8, 400))
    assert not ticks[0]
    assert all(ticks[20:]), "a hand wobbling 1.6 peak-to-peak counts from the first second on"
    assert all(feed_ticks(song_plus(), resting_hand(-1.1, 0.8, 400))[20:]), "measured from the zero's side too"

  def test_a_wobble_under_the_activity_level_does_not_count(self):
    assert not any(feed_ticks(song_plus(), resting_hand(2.64, 0.6, 400)))   # 1.2 peak-to-peak

  def test_a_steady_offset_on_a_still_wheel_is_not_a_hand(self):
    # A quiet wheel is judged on activity only, so a constant torque - a zero that has drifted, a
    # residue after a turn - cannot hide the alerts however far it is from the zero. A real hand
    # is never that still (vendor lateral control: activity in 94 % of ticks).
    assert not any(feed_ticks(song_plus(), [3.41] * 400))

  def test_the_eps_turning_the_wheel_hands_off_is_not_a_touch(self):
    # The angle's 0.1 deg steps put 8-20 deg/s^2 rms of noise on thetaddot (up to 41), so these
    # stay clear of quiet_accel: near it the branch may change from one tick to the next.
    # quiet wheel, thetaddot up to 69 deg/s^2
    torques, angles, peak = eps_turning(7., 0.5, 600)
    assert peak < HANDS_ON_TORQUE[SONG_PLUS]["quiet_accel"] - 25
    assert not any(feed_ticks(song_plus(), torques, angles))
    # wheel moving fast, thetaddot up to 395 deg/s^2: the inertia alone reads |torque - zero| 3.4
    torques, angles, peak = eps_turning(10., 1.0, 600)
    assert peak > HANDS_ON_TORQUE[SONG_PLUS]["quiet_accel"] + 25
    assert max(abs(x - ZERO) for x in torques) > 3.0
    assert not any(feed_ticks(song_plus(), torques, angles))
    no_inertia = HandsOnTorque(**{**HANDS_ON_TORQUE[SONG_PLUS], "inertia": 0.0})
    assert any(feed_ticks(no_inertia, torques, angles)), "without the inertia term this is a touch"

  def test_a_hand_on_a_moving_wheel_counts(self):
    torques, angles, _ = eps_turning(5., 0.5, 600)       # quiet: judged on activity
    hand = resting_hand(0., 1.0, 600, hz=3.0)
    assert all(feed_ticks(song_plus(), [x + h for x, h in zip(torques, hand, strict=True)], angles)[20:])
    torques, angles, _ = eps_turning(10., 1.0, 600)      # fast: judged on |c| > 3.0
    assert all(feed_ticks(song_plus(), [x + 4.5 for x in torques], angles)[20:])

  def test_on_a_moving_wheel_a_touch_must_last_two_ticks(self):
    torques, angles, _ = eps_turning(10., 1.0, 600)
    short = [x + (4.5 if 300 <= i < 305 else 0.) for i, x in enumerate(torques)]   # one tick long
    assert not any(feed_ticks(song_plus(), short, angles))
    held = [x + (4.5 if 300 <= i < 315 else 0.) for i, x in enumerate(torques)]    # three ticks
    assert any(feed_ticks(song_plus(), held, angles))

  def test_no_new_carstate_resets(self):
    det = song_plus()
    assert feed_ticks(det, resting_hand(2.64, 0.8, 200))[-1]
    assert det.tick(fresh=False) is False and det.ticks == 0
    assert det.tick() is False             # counts again from one
    assert det.tick() is True

  def test_a_pause_in_carstate_starts_over(self):
    det = song_plus()
    assert feed_ticks(det, resting_hand(2.64, 0.8, 200))[-1]
    det.feed(200 * SAMPLE_DT + GAP_S + 0.05, 2.64, 0.)
    assert len(det._window) == 0 and det.tick() is False


class FakeCarState:
  def __init__(self):
    self.steeringPressed = False
    self.gasPressed = False
    self.vEgo = 8.3


class FakeSubMaster:
  def __init__(self):
    self.cs = FakeCarState()
    self.updates = 0

  def update(self, timeout=100):
    self.updates += 1

  def __getitem__(self, service):
    assert service == 'carState'
    return self.cs


class ScriptedFeed:
  """Stands in for carstate_feed(): each call returns the carStates of one DM tick (5 at 100 Hz)."""
  def __init__(self, torques, ticks_with_data=None):
    samples = [(i * SAMPLE_DT, tq, 0.) for i, tq in enumerate(torques)]
    self._batches = [samples[i:i + 5] for i in range(0, len(samples), 5)][:ticks_with_data]

  def __call__(self):
    return self._batches.pop(0) if self._batches else []


class TestSubMasterNoCabinCamera(OpenpilotTestCase):
  def test_steering_pressed_is_widened_for_monitoring_only(self):
    det = song_plus()
    real = FakeSubMaster()
    sm = SubMasterNoCabinCamera(real, no_face_driver_state(), det, ScriptedFeed(resting_hand(2.64, 0.8, 300)))
    seen = []
    for _ in range(60):
      sm.update(0)
      seen.append(sm['carState'].steeringPressed)
    assert seen[0] is False and all(seen[20:])
    cs = sm['carState']
    assert cs.vEgo == 8.3 and cs.gasPressed is False       # everything else passes through
    assert real['carState'].steeringPressed is False       # the real carState is untouched
    ticks = det.ticks
    for _ in range(5):                                     # reads do not advance the detector
      _ = sm['carState']
    assert det.ticks == ticks

  def test_a_stalled_carstate_is_not_a_held_touch(self):
    det = song_plus()
    sm = SubMasterNoCabinCamera(FakeSubMaster(), no_face_driver_state(), det,
                                ScriptedFeed(resting_hand(2.64, 0.8, 300), ticks_with_data=40))
    for _ in range(40):
      sm.update(0)
    assert sm['carState'].steeringPressed is True
    sm.update(0)                                           # no carState since the last tick
    assert sm['carState'].steeringPressed is False
    assert det.ticks == 0

  def test_without_a_detector_carstate_is_the_real_one(self):
    real = FakeSubMaster()
    sm = SubMasterNoCabinCamera(real, no_face_driver_state())
    sm.update(0)
    assert sm['carState'] is real.cs

  def test_carstate_feed_returns_every_message_since_the_last_call(self):
    from unittest import mock
    from openpilot.selfdrive.monitoring import dmonitoringd

    def msg(mono_ns, torque, angle):
      m = mock.Mock()
      m.logMonoTime, m.carState.steeringTorque, m.carState.steeringAngleDeg = mono_ns, torque, angle
      return m
    queue = [[msg(1_000_000_000, 2.5, -3.0), msg(1_010_000_000, 2.6, -3.1)], []]
    with mock.patch.object(dmonitoringd.messaging, 'drain_sock', side_effect=lambda sock: queue.pop(0)):
      drain = carstate_feed('sock')
      assert drain() == [(1.0, 2.5, -3.0), (1.01, 2.6, -3.1)]
      assert drain() == []

  def test_dmonitoringd_wires_the_detector_and_keeps_steering_pressed_at_24(self):
    import ast
    import inspect
    from openpilot.selfdrive.monitoring import dmonitoringd
    from opendbc.car.byd.values import CarControllerParams
    tree = ast.parse(inspect.getsource(dmonitoringd.dmonitoringd_thread))
    proxies = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'SubMasterNoCabinCamera']
    assert proxies and all(len(n.args) == 4 for n in proxies), "the hands-on detector and its carState feed are not passed in"
    feed = ast.unparse(proxies[0].args[3])
    assert "carstate_feed" in feed and "sub_sock('carState', conflate=False)" in feed, \
      f"the detector needs every carState, from its own unconflated socket; got {feed}"
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert 'HandsOnTorque.for_car' in calls
    # The decoupling: the port's steeringPressed stays a deliberate-override signal, because it
    # also drives the lateral override state, the lane-change nudge and the port's release of
    # STEER_REQ while the driver overrides. Lowering it is a separate decision.
    assert CarControllerParams.STEER_DRIVER_OVERRIDE_NM == 24.0
