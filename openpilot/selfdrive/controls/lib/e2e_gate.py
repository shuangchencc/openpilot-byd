"""When the driving model's own acceleration may take part in the longitudinal planner's minimum (BYD Song PLUS only).

The planner takes the lowest of three candidates: the MPC on a lead the camera sees, the set speed ("cruise"), and the
model's own wish, modelV2.action.desiredAcceleration ("e2e"). On the Song PLUS openpilot can only lower the stock ACC's
request (opendbc byd/values.py, SongPlusLongitudinal), so the stock ACC is the upper bound, and whenever the model's
sense of how fast this road should be driven is below the set speed it holds the car there: on 2026-11-15 the car went
no faster than 60-70 km/h with a high set speed and nothing ahead. The owner's rule: the set speed, unless a big curve
is ahead or the model means to stop. So the model's wish takes part only
  - before a big curve: within the next CURVE_HORIZON s, the path the model plans bends so that driving it at the
    current speed would take more than CURVE_LAT_ACCEL of lateral acceleration. The curvature at each point of the
    plan is its yaw rate over its speed there; the plan's own lateral acceleration would understate the curve, since
    its speeds are already the slowed ones (a0's review);
  - when the model means to stop: the speed it plans falls below STOP_PLAN_SPEED within STOP_HORIZON s (its own
    action.shouldStop only turns true below 0.3 m/s, when the car has all but stopped);
While a lane change is under way (modelV2.meta.laneChangeState not off) the curve has to be bigger,
CURVE_LAT_ACCEL_LANE_CHANGE: the lane change's own brief yaw read 1.6 m/s^2 at 80 km/h on 2026-11-15 (route 0000002e,
3 of 16 lane changes; a0's review), which is no curve to slow for. And all of it for HOLD_S after either was last
true, so that the curve or the stop is not let go of and taken up again.
The MPC and the set speed take part as always. Design and data: migration-audit/longitudinal-speedcap-20261115.
"""
import numpy as np

from openpilot.common.realtime import DT_MDL

CURVE_LAT_ACCEL = 1.5    # m/s^2
CURVE_LAT_ACCEL_LANE_CHANGE = 2.5    # m/s^2, while a lane change is under way
CURVE_HORIZON = 5.0      # s
STOP_PLAN_SPEED = 2.0    # m/s
STOP_HORIZON = 10.0      # s
HOLD_S = 2.0             # s
MIN_PLAN_SPEED = 1.0     # m/s; the plan's yaw rate over a speed below this is no curvature worth reading


class E2EGate:
  def __init__(self, dt: float = DT_MDL):
    self.hold_frames = round(HOLD_S / dt)
    self.frames_left = 0     # the model's wish takes part while this is above 0
    self.curve = False       # what opened it in this step, for the log and for tests
    self.stop = False
    self.lat_accel_ahead = 0.0   # m/s^2, the largest lateral acceleration the plan's path would take at the current speed
    self.min_plan_speed = 0.0    # m/s, the lowest speed the model plans within STOP_HORIZON

  def update(self, model, v_ego: float) -> bool:
    """Once per planner step: may the model's acceleration take part in this step's minimum?"""
    t = np.asarray(model.orientationRate.t, dtype=float)
    yaw_rate = np.asarray(model.orientationRate.z, dtype=float)
    v_plan = np.asarray(model.velocity.x, dtype=float)
    self.curve = self.stop = False
    if len(t) and len(t) == len(yaw_rate) == len(v_plan):
      ahead = t <= CURVE_HORIZON
      curvature = np.abs(yaw_rate[ahead]) / np.maximum(v_plan[ahead], MIN_PLAN_SPEED)
      self.lat_accel_ahead = float(np.max(curvature) * v_ego ** 2)
      self.min_plan_speed = float(np.min(v_plan[t <= STOP_HORIZON]))
      lane_change = str(getattr(getattr(model, "meta", None), "laneChangeState", "off")) != "off"
      self.curve = self.lat_accel_ahead > (CURVE_LAT_ACCEL_LANE_CHANGE if lane_change else CURVE_LAT_ACCEL)
      self.stop = self.min_plan_speed < STOP_PLAN_SPEED
    else:
      # no plan to judge by: as without the gate
      self.curve = True
    if self.curve or self.stop:
      self.frames_left = self.hold_frames
    else:
      self.frames_left = max(self.frames_left - 1, 0)
    return self.curve or self.stop or self.frames_left > 0
