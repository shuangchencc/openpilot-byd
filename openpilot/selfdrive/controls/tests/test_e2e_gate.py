import unittest
from types import SimpleNamespace

import numpy as np

from opendbc.car.structs import car
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib import e2e_gate
from openpilot.selfdrive.controls.lib.e2e_gate import E2EGate
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
from openpilot.selfdrive.modeld.constants import ModelConstants

T = np.array(ModelConstants.T_IDXS)


def plan(v_plan, yaw_rate=None, lane_change="off"):
  """A modelV2 with only what the gate reads: planned speeds and yaw rates at the model's times."""
  v_plan = np.broadcast_to(np.asarray(v_plan, dtype=float), T.shape)
  yaw_rate = np.zeros_like(T) if yaw_rate is None else np.broadcast_to(np.asarray(yaw_rate, dtype=float), T.shape)
  return SimpleNamespace(orientationRate=SimpleNamespace(t=T.tolist(), z=yaw_rate.tolist()), velocity=SimpleNamespace(x=v_plan.tolist()),
                         meta=SimpleNamespace(laneChangeState=lane_change))


def curve_at(t0, v, lat_accel):
  """Yaw rates for a curve that starts t0 s ahead and, at speed v, takes lat_accel of lateral acceleration."""
  return np.where(T >= t0, lat_accel / v, 0.0)


class TestE2EGate(unittest.TestCase):
  def test_shut_on_a_straight_road(self):
    g = E2EGate()
    for _ in range(100):
      self.assertFalse(g.update(plan(20.0, np.full(T.shape, 0.002)), 20.0))   # lane keeping wiggle: 0.04 m/s^2

  def test_open_before_a_big_curve(self):
    for t0, lat, expected in ((1.0, 2.0, True), (4.5, 1.6, True), (1.0, 1.4, False), (6.0, 3.0, False)):
      with self.subTest(t0=t0, lat=lat):
        g = E2EGate()
        self.assertEqual(expected, g.update(plan(20.0, curve_at(t0, 20.0, lat)), 20.0))
        self.assertEqual(expected, g.curve)

  def test_the_curve_is_judged_at_the_current_speed(self):
    # the plan slows to 12 m/s for a 100 m curve (1.44 m/s^2 at the plan's speed); at 20 m/s it would be 4 m/s^2
    v_plan = np.interp(T, [0, 3, 10], [20, 12, 12])
    yaw = np.where(T >= 3, 12.0 / 100.0, 0.0)
    g = E2EGate()
    self.assertTrue(g.update(plan(v_plan, yaw), 20.0))
    self.assertTrue(g.curve)

  def test_a_lane_change_needs_a_bigger_curve(self):
    for state, lat, expected in (("laneChangeStarting", 1.7, False), ("preLaneChange", 2.0, False), ("laneChangeFinishing", 2.7, True),
                                 ("off", 1.7, True)):
      with self.subTest(state=state, lat=lat):
        g = E2EGate()
        self.assertEqual(expected, g.update(plan(22.0, curve_at(1.0, 22.0, lat), state), 22.0))

  def test_open_when_the_model_means_to_stop(self):
    for v_end, expected in ((0.0, True), (1.9, True), (2.1, False)):
      with self.subTest(v_end=v_end):
        g = E2EGate()
        self.assertEqual(expected, g.update(plan(np.interp(T, [0, 8, 10], [14, v_end, v_end])), 14.0))
        self.assertEqual(expected, g.stop)
    # a stop planned beyond the horizon the model gives does not count; the plan ends at 10 s
    self.assertEqual(e2e_gate.STOP_HORIZON, 10.0)

  def test_holds_for_two_seconds(self):
    g = E2EGate()
    self.assertTrue(g.update(plan(20.0, curve_at(0.0, 20.0, 2.5)), 20.0))
    open_frames = 0
    while g.update(plan(20.0), 20.0):
      open_frames += 1
      self.assertLess(open_frames, 1000)
    self.assertEqual(open_frames, round(e2e_gate.HOLD_S / DT_MDL) - 1)
    # and a new curve opens it again at once
    self.assertTrue(g.update(plan(20.0, curve_at(0.0, 20.0, 2.5)), 20.0))

  def test_no_plan_is_as_without_the_gate(self):
    g = E2EGate()
    self.assertTrue(g.update(SimpleNamespace(orientationRate=SimpleNamespace(t=[], z=[]), velocity=SimpleNamespace(x=[])), 20.0))


class FakeSM(dict):
  def __init__(self, msgs):
    super().__init__(msgs)
    self.logMonoTime = dict.fromkeys(msgs, 0)

  def all_checks(self, *_):
    return True


def planner_inputs(model):
  from openpilot.cereal import messaging
  msgs = {}
  for name in ("carState", "controlsState", "radarState", "selfdriveState", "carControl"):
    msgs[name] = getattr(messaging.new_message(name), name)
  cs = messaging.new_message("carState")
  cs.carState.vEgo, cs.carState.vCruise = 22.0, 120.0
  msgs["carState"] = cs.carState
  ss = messaging.new_message("selfdriveState")
  ss.selfdriveState.enabled = True
  msgs["selfdriveState"] = ss.selfdriveState
  m = messaging.new_message("modelV2")
  m.modelV2.orientationRate.t = T.tolist()
  m.modelV2.orientationRate.z = list(model.orientationRate.z)
  m.modelV2.velocity.x = list(model.velocity.x)
  m.modelV2.action.desiredAcceleration = -0.6       # the model wants less than the set speed does
  msgs["modelV2"] = m.modelV2
  return FakeSM(msgs)


class TestPlannerUsesTheGate(unittest.TestCase):
  """a0's review: with the model's wish the lowest, the source is e2e only while the gate is open."""

  def test_the_models_wish_counts_only_before_a_curve(self):
    CP = car.CarParams(carFingerprint="BYD_SONG_PLUS_DMI_23", openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.3)
    planner = LongitudinalPlanner(CP)
    for _ in range(60):     # 3 s of a straight road: the set speed's candidate, not the model's
      planner.update(planner_inputs(plan(22.0)))
    self.assertNotEqual(planner.mpc.source, 4)
    self.assertGreater(planner.output_a_target, -0.6)
    planner.update(planner_inputs(plan(22.0, curve_at(2.0, 22.0, 2.5))))
    self.assertEqual(planner.mpc.source, 4)               # LongitudinalPlanSource.e2e
    self.assertAlmostEqual(planner.output_a_target, -0.6, places=3)
    # and elsewhere the model's wish counts as it always has
    other = LongitudinalPlanner(car.CarParams(carFingerprint="TOYOTA_COROLLA_TSS2", openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.3))
    other.update(planner_inputs(plan(22.0)))
    self.assertEqual(other.mpc.source, 4)


class TestPlannerUsesTheGateOnlyOnTheSongPlus(unittest.TestCase):
  def test_only_the_song_plus(self):
    for fingerprint, gated in (("BYD_SONG_PLUS_DMI_23", True), ("BYD_ATTO_3", False), ("TOYOTA_COROLLA_TSS2", False), ("", False)):
      CP = car.CarParams(carFingerprint=fingerprint)
      self.assertEqual(gated, LongitudinalPlanner(CP).e2e_gate is not None, fingerprint)


if __name__ == "__main__":
  unittest.main()
