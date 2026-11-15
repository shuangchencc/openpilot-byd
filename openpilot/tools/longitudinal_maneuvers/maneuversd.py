#!/usr/bin/env python3
import numpy as np
from dataclasses import dataclass

from opendbc.car.structs import car
from openpilot.cereal import messaging
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.controls.lib.drive_helpers import should_stop


@dataclass
class Action:
  accel_bp: list[float]  # m/s^2
  time_bp: list[float]   # seconds

  def __post_init__(self):
    assert len(self.accel_bp) == len(self.time_bp)


@dataclass
class Maneuver:
  description: str
  actions: list[Action]
  repeat: int = 0
  initial_speed: float = 0.  # m/s
  # how close to initial_speed counts as being there, and what is asked for while getting there:
  # None steers towards initial_speed (see SONGPLUS_MANEUVERS for why not always)
  speed_tolerance: float = 0.3  # m/s
  setup_accel: float | None = None  # m/s^2

  _active: bool = False
  _finished: bool = False
  _run_completed: bool = False
  _action_index: int = 0
  _action_frames: int = 0
  _ready_cnt: int = 0
  _repeated: int = 0

  def _step(self) -> float:
    self._run_completed = False
    action = self.actions[self._action_index]
    action_accel = np.interp(self._action_frames * DT_MDL, action.time_bp, action.accel_bp)

    self._action_frames += 1

    # reached duration of action
    if self._action_frames > (action.time_bp[-1] / DT_MDL):
      # next action
      if self._action_index < len(self.actions) - 1:
        self._action_index += 1
        self._action_frames = 0
      # repeat maneuver
      elif self._repeated < self.repeat:
        self._repeated += 1
        self._run_completed = True
        self.reset()
      # finish maneuver
      else:
        self._run_completed = True
        self._finished = True

    return float(action_accel)

  def get_accel(self, v_ego: float, long_active: bool, standstill: bool, cruise_standstill: bool, /) -> float:
    ready = abs(v_ego - self.initial_speed) < self.speed_tolerance and long_active and not cruise_standstill
    if self.initial_speed < 0.01:
      ready = ready and standstill
    self._ready_cnt = (self._ready_cnt + 1) if ready else 0

    if self._ready_cnt > (3. / DT_MDL):
      self._active = True

    if not self._active:
      if self.setup_accel is not None:
        return self.setup_accel
      return min(max(self.initial_speed - v_ego, -2.), 2.)

    return self._step()

  def reset(self):
    self._active = False
    self._action_frames = 0
    self._action_index = 0

  @property
  def finished(self):
    return self._finished

  @property
  def active(self):
    return self._active


MANEUVERS = [
  Maneuver(
    "come to stop",
    [Action([-0.5], [12])],
    repeat=2,
    initial_speed=5.,
  ),
  Maneuver(
    "start from stop",
    [Action([1.5], [6])],
    repeat=2,
    initial_speed=0.,
  ),
  Maneuver(
    "creep: alternate between +1m/s^2 and -1m/s^2",
    [
      Action([1], [3]), Action([-1], [3]),
      Action([1], [3]), Action([-1], [3]),
      Action([1], [3]), Action([-1], [3]),
    ],
    repeat=2,
    initial_speed=0.,
  ),
  Maneuver(
    "brake step response: -1m/s^2 from 20mph",
    [Action([-1], [3])],
    repeat=2,
    initial_speed=20. * CV.MPH_TO_MS,
  ),
  Maneuver(
    "brake step response: -3.5m/s^2 from 20mph",
    [Action([-3.5], [3])],
    repeat=2,
    initial_speed=20. * CV.MPH_TO_MS,
  ),
  Maneuver(
    "gas step response: +1m/s^2 from 20mph",
    [Action([1], [3])],
    repeat=2,
    initial_speed=20. * CV.MPH_TO_MS,
  ),
  Maneuver(
    "gas step response: +2m/s^2 from 20mph",
    [Action([2], [3])],
    repeat=2,
    initial_speed=20. * CV.MPH_TO_MS,
  ),
]


def _square(amplitude: float, period: float, duration: float) -> Action:
  """-amplitude for the first half of every period and +amplitude for the second, for `duration` s."""
  accel_bp, time_bp = [], []
  for i in range(round(duration / period)):
    t = i * period
    accel_bp += [-amplitude, -amplitude, amplitude, amplitude]
    time_bp += [t, t + period / 2 - 0.001, t + period / 2, t + period - 0.001]
  return Action(accel_bp, time_bp)


# BYD Song PLUS with openpilot longitudinal (migration-audit/longitudinal-l1-20261114/DESIGN.txt 7.3, "L4"): there
# openpilot can only make the car ask for LESS than the stock ACC, which stays engaged and keeps its set speed, and
# panda takes no request below -1.0 m/s^2. The maneuvers above stop the car, start it, or ask for more than the stock
# ACC does, none of which it can do. These are steps and swings below the stock request - about 0 at a steady speed -
# from anywhere between 28 and 42 km/h (the driver sets the ACC so that the car holds about 35). Before each run, and
# after the last, +2 m/s^2 is asked for: never below the stock request, so the stock ACC alone brings the car back to
# its set speed, as it would without openpilot.
SONGPLUS_SETUP = {"initial_speed": 35. * CV.KPH_TO_MS, "speed_tolerance": 2.0, "setup_accel": 2.0}
SONGPLUS_IDLE_ACCEL = 2.0
SONGPLUS_MANEUVERS = [
  Maneuver("L4 step 1: -0.3 m/s^2 for 3 s", [Action([-0.3], [3])], repeat=1, **SONGPLUS_SETUP),
  Maneuver("L4 step 2: -0.5 m/s^2 for 3 s", [Action([-0.5], [3])], repeat=1, **SONGPLUS_SETUP),
  Maneuver("L4 step 2: -1.0 m/s^2 for 3 s", [Action([-1.0], [3])], repeat=1, **SONGPLUS_SETUP),
  Maneuver("L4 step 3: slow swing +0.3 .. -0.3 m/s^2, 60 s", [Action([0.3, -0.3] * 6 + [0.3], [5. * i for i in range(13)])], **SONGPLUS_SETUP),
  Maneuver("L4 step 4: +-0.1 m/s^2 at 5 Hz, 10 s", [_square(0.1, 0.2, 10.)], **SONGPLUS_SETUP),
  Maneuver("L4 step 4: +-0.3 m/s^2 at 5 Hz, 10 s", [_square(0.3, 0.2, 10.)], **SONGPLUS_SETUP),
  Maneuver("L4 step 4: +-0.5 m/s^2 at 5 Hz, 10 s", [_square(0.5, 0.2, 10.)], **SONGPLUS_SETUP),
  Maneuver("L4 step 5: -0.3 m/s^2 for 10 s, the host fails in the middle", [Action([-0.3], [10])], **SONGPLUS_SETUP),
]

# Level 2, floor -2.0 m/s^2 ("L5", the same DESIGN.txt 7.4): steps 1-2 of L4 beyond level 1's floor, from 50 km/h so that
# 3 s at -2.0 m/s^2 still ends above 25 km/h (the driver sets the ACC so that the car holds about 50).
SONGPLUS_L5_SETUP = {"initial_speed": 50. * CV.KPH_TO_MS, "speed_tolerance": 2.0, "setup_accel": 2.0}
SONGPLUS_L5_MANEUVERS = [
  Maneuver("L5 step 1: -1.0 m/s^2 for 3 s", [Action([-1.0], [3])], **SONGPLUS_L5_SETUP),
  Maneuver("L5 step 2: -1.5 m/s^2 for 3 s", [Action([-1.5], [3])], repeat=1, **SONGPLUS_L5_SETUP),
  Maneuver("L5 step 2: -2.0 m/s^2 for 3 s", [Action([-2.0], [3])], repeat=1, **SONGPLUS_L5_SETUP),
]
# by the longitudinal level in the safety param (bits 1-2); any other level gets level 1's list, which stays above every floor
SONGPLUS_MANEUVERS_BY_LEVEL = {1: SONGPLUS_MANEUVERS, 2: SONGPLUS_L5_MANEUVERS}


def main():
  params = Params()
  cloudlog.info("maneuversd is waiting for CarParams")
  CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  songplus = CP.carFingerprint == "BYD_SONG_PLUS_DMI_23"
  level = (CP.safetyConfigs[0].safetyParam >> 1) & 3 if len(CP.safetyConfigs) else 0

  sm = messaging.SubMaster(['carState', 'carControl', 'controlsState', 'selfdriveState', 'modelV2'], poll='modelV2')
  pm = messaging.PubMaster(['longitudinalPlan', 'driverAssistance', 'alertDebug'])

  maneuvers = iter(SONGPLUS_MANEUVERS_BY_LEVEL.get(level, SONGPLUS_MANEUVERS) if songplus else MANEUVERS)
  maneuver = None

  while True:
    sm.update()

    if maneuver is None:
      maneuver = next(maneuvers, None)

    alert_msg = messaging.new_message('alertDebug')
    alert_msg.valid = True

    plan_send = messaging.new_message('longitudinalPlan')
    plan_send.valid = sm.all_checks()

    longitudinalPlan = plan_send.longitudinalPlan
    accel = SONGPLUS_IDLE_ACCEL if songplus else 0
    v_ego = max(sm['carState'].vEgo, 0)

    if maneuver is not None:
      accel = maneuver.get_accel(v_ego, sm['carControl'].longActive, sm['carState'].standstill, sm['carState'].cruiseState.standstill)

      if maneuver.active:
        alert_msg.alertDebug.alertText1 = f'Maneuver Active: {accel:0.2f} m/s^2'
      else:
        alert_msg.alertDebug.alertText1 = f'Setting up to {maneuver.initial_speed * CV.MS_TO_MPH:0.2f} mph'
      alert_msg.alertDebug.alertText2 = f'{maneuver.description}'
    else:
      alert_msg.alertDebug.alertText1 = 'Maneuvers Finished'

    pm.send('alertDebug', alert_msg)

    longitudinalPlan.aTarget = accel
    longitudinalPlan.shouldStop = should_stop(v_ego, accel)

    longitudinalPlan.allowBrake = True
    longitudinalPlan.hasLead = True

    longitudinalPlan.speeds = [0.2]  # triggers carControl.cruiseControl.resume in controlsd

    pm.send('longitudinalPlan', plan_send)

    assistance_send = messaging.new_message('driverAssistance')
    assistance_send.valid = True
    pm.send('driverAssistance', assistance_send)

    if maneuver is not None and maneuver.finished:
      maneuver = None
