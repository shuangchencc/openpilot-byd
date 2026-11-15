import math
from collections import deque

import numpy as np

from opendbc.can.packer import CANPacker
from opendbc.car import Bus, DT_CTRL
from opendbc.car.lateral import apply_steer_angle_limits_vm, get_max_angle_delta_vm, get_max_angle_vm
from opendbc.car.interfaces import CarControllerBase
from opendbc.car.byd import bydcan
from opendbc.car.byd.carstate import EPS_LKA_ACTIVE
from opendbc.car.byd.values import CAR, BydFlags, CarControllerParams, SAFETY_VM_PLATFORM, SONG_PLUS_SAFETY_SLIP_FACTOR, \
                                   SONG_PLUS_SAFETY_STEER_RATIO, SongPlusLongitudinal as Long
from opendbc.car.vehicle_model import VehicleModel


def get_safety_CP(platform=SAFETY_VM_PLATFORM):
  """The vehicle model panda's byd.h checks steering angles with, for this platform; the controller must limit with
  the same one (values.py, SONG_PLUS_SAFETY_STEER_RATIO)."""
  from opendbc.car.byd.interface import CarInterface
  if platform != CAR.BYD_SONG_PLUS_DMI_23:
    return CarInterface.get_non_essential_params(SAFETY_VM_PLATFORM)
  CP = CarInterface.get_non_essential_params(CAR.BYD_SONG_PLUS_DMI_23)
  CP.steerRatio = SONG_PLUS_SAFETY_STEER_RATIO
  # the front tyre stiffness that gives the slip factor: (cF aF - cR aR) / (L^2 cF cR), all per unit mass
  a_f, a_r, c_r = CP.centerToFront, CP.wheelbase - CP.centerToFront, CP.tireStiffnessRearPerMass
  CP.tireStiffnessFrontPerMass = c_r * a_r / (a_f - SONG_PLUS_SAFETY_SLIP_FACTOR * CP.wheelbase ** 2 * c_r)
  return CP


class CarController(CarControllerBase):
  def __init__(self, dbc_names, CP):
    super().__init__(dbc_names, CP)
    self.packer = CANPacker(dbc_names[Bus.pt])
    self.apply_angle_last = 0.0

    # Vehicle model used for lateral limiting
    self.VM = VehicleModel(get_safety_CP(CP.carFingerprint))

    # Steering-command constants this car's EPS expects, measured from accepted frames.
    # ANGLE_RATE_LIMIT is `engaged` while asking the EPS to steer and `idle` while not,
    # and moves between the two by `step` per frame. See BydFlags.TX_CONSTANTS_ALT.
    if CP.flags & BydFlags.TX_CONSTANTS_ALT:
      self.rate_limit_values = {'engaged': 500, 'idle': 300, 'step': 16}
      self.set_me_f = 0
    else:
      self.rate_limit_values = {'engaged': 299, 'idle': 0, 'step': 299}
      self.set_me_f = 0xF
    self.rate_limit = self.rate_limit_values['idle']

    # See BydFlags.NO_CANCEL_BUTTON
    self.send_cancel_button = not (CP.flags & BydFlags.NO_CANCEL_BUTTON)

    # See BydFlags.YIELD_TO_DRIVER
    self.yield_to_driver = bool(CP.flags & BydFlags.YIELD_TO_DRIVER)
    self.yield_frames = 0
    self.lane_change_push_frames = 0
    self.smoothed_angle = 0.0
    self.request_frames = 0
    self.steer_req_last = False

    # See BydFlags.LKAS_HUD_ALT
    self.song_plus_hud = bool(CP.flags & BydFlags.LKAS_HUD_ALT)

    # See BydFlags.EPS_LKA_STATE
    self.watch_eps_answer = bool(CP.flags & BydFlags.EPS_LKA_STATE)
    self.unanswered_frames = 0
    self.request_unanswered = False
    self.unanswered_reported_frames = 0
    self.rejected_frames: deque[int] = deque()
    # The counter of the last steering frame built, and whether panda refused that frame.
    # The counter only moves on once a frame has made it onto the bus: when the EPS
    # misses one, it went on steering for another 250 ms after we had stopped asking
    # (twice, both times a brake press, where panda refuses the frame already built),
    # against 30 ms whenever no frame was missing. A refused frame is therefore sent
    # again under its own counter in the next slot, and the cluster frame, which the
    # stock camera keeps in step with it and which panda did not refuse, skips that
    # slot so that both counters stay unbroken and equal.
    self.counter = 15
    self.steer_frame_refused = False

    # See SongPlusLongitudinal
    self.acc_cmd_counter_last = -1
    self.acc_warning_until_nanos = 0

  def acc_cmd_replacement(self, CC, CS, now_nanos):
    """What the car is asked for in this step when openpilot does the longitudinal control.

    Returns (frame to send or None, the request the car gets [m/s^2]). A frame is sent only
    when there is a stock frame that has not been answered yet, that frame may be copied,
    and openpilot wants less than it asks for. The conditions are byd.h's, on the same
    stock frame, so that panda refuses nothing we send except a frame that comes too late
    or one sent in the moment the driver touched a pedal.
    """
    stock = CS.acc_cmd
    # the stock frames count up by one, 20 ms apart: another counter is another frame
    new_stock_frame = stock["COUNTER"] != self.acc_cmd_counter_last
    self.acc_cmd_counter_last = stock["COUNTER"]

    if CS.acc_warning:
      self.acc_warning_until_nanos = now_nanos + Long.WARNING_HOLD_NANOS
    warning = CS.acc_warning or now_nanos < self.acc_warning_until_nanos

    # what the car gets whenever no frame of ours stands in for the stock one
    stock_accel = (stock["REQUEST"] - Long.REQUEST_ZERO) * Long.REQUEST_SCALE
    copyable = (CS.acc_state_code == Long.CONTROLLING_STATE_CODE and not warning and
                stock["STOCK_BYTE_5"] == Long.CONTROLLING_BYTE_5 and not stock["STOCK_BYTES_1_4"] >> 31)   # bit 39
    if not (CC.longActive and copyable and math.isfinite(CC.actuators.accel)):
      return None, stock_accel

    # the nearest count to what openpilot wants, but never below this level's floor
    request = max(round(CC.actuators.accel / Long.REQUEST_SCALE) + Long.REQUEST_ZERO, Long.MIN_REQUEST[Long.LEVEL])
    if request >= stock["REQUEST"]:
      return None, stock_accel          # the stock ACC wants no more than we do: its frames go to the car
    accel = (request - Long.REQUEST_ZERO) * Long.REQUEST_SCALE
    if not new_stock_frame:
      return None, accel                # already answered; the next stock frame is 20 ms after the last
    msg = bydcan.create_acc_cmd(self.packer, request, stock["STOCK_BYTES_1_4"], stock["STOCK_BYTE_5"], (stock["COUNTER"] + 1) % 16)
    return msg, accel

  @staticmethod
  def lane_change_push(CC, CS):
    """While openpilot shows a lane change, steeringPressed is the push that starts it, not the driver taking
    the wheel, unless the torque is firm (STEER_YIELD_LANE_CHANGE_PUSH_MAX) or above the steeringPressed
    threshold against the lane change. Below that threshold either way it is the push easing off:
    steeringPressed outlasts the torque by a few frames, and a released wheel reads about -1.1. How long
    the push may be firm is update()'s (STEER_YIELD_LANE_CHANGE_PUSH_FRAMES, STEER_YIELD_LANE_CHANGE_HELD_TORQUE).
    Positive torque is the wheel turned left, as for openpilot's DesireHelper, which takes the same sign to
    start a change to the left."""
    side = 1 if CC.leftBlinker else -1 if CC.rightBlinker else 0
    torque = CS.out.steeringTorque
    firm = abs(torque) >= CarControllerParams.STEER_YIELD_LANE_CHANGE_PUSH_MAX
    against = torque * side < -CarControllerParams.STEER_DRIVER_OVERRIDE_NM
    return side != 0 and not firm and not against

  def update(self, CC, CS, now_nanos):
    can_sends = []
    actuators = CC.actuators
    hud_control = CC.hudControl

    # Whether the EPS is asked to steer in this step. openpilot wanting to (latActive)
    # is not enough while the driver is turning the wheel: this EPS holds the commanded
    # angle against him with all the torque it has, and gives up for the rest of the
    # drive when that has lasted 0.7 s (see BydFlags.YIELD_TO_DRIVER). So the request
    # is withdrawn while steeringPressed and for a moment after, exactly as if openpilot
    # were not steering: STEER_REQ 0, the commanded angle following the wheel, so that
    # the next request starts from where the wheel then is. The push that starts a lane change openpilot
    # shows is not such a turn (lane_change_push).
    steer_req = CC.latActive
    if self.yield_to_driver:
      push = CS.out.steeringPressed and self.lane_change_push(CC, CS)
      if not push:
        self.lane_change_push_frames = 0
      elif abs(CS.out.steeringTorque) >= CarControllerParams.STEER_YIELD_LANE_CHANGE_HELD_TORQUE:
        self.lane_change_push_frames += 1          # only the push's firm frames count towards its half second
      push = push and self.lane_change_push_frames <= CarControllerParams.STEER_YIELD_LANE_CHANGE_PUSH_FRAMES
      if CS.out.steeringPressed and not push:
        self.yield_frames = CarControllerParams.STEER_YIELD_HOLD_FRAMES
      elif self.yield_frames > 0:
        self.yield_frames -= 1
      steer_req = steer_req and self.yield_frames == 0

      # A request only starts with the wheel inside the angle the lateral acceleration
      # limit allows at this speed (18 deg at 90 km/h). Outside it there is no angle we
      # could ask for: the limit's own would be a jump from the wheel, the wheel's own is
      # beyond the limit, and panda rejects either, frame after frame, leaving the EPS
      # with no steering frame at all. So we go on sending "not steering" until the
      # wheel is back inside. A request that is already running is not ended by this.
      if steer_req and not self.steer_req_last:
        max_angle = get_max_angle_vm(max(CS.out.vEgoRaw, 1), self.VM, CarControllerParams)
        steer_req = abs(CS.out.steeringAngleDeg) < CarControllerParams.STEER_REQUEST_START_ANGLE_RATIO * max_angle
      self.steer_req_last = steer_req

    if self.watch_eps_answer and self.counter in CS.steer_counters_rejected:
      self.steer_frame_refused = True

    if self.frame % 2:
      target = actuators.steeringAngleDeg
      if self.yield_to_driver:
        # openpilot's angle, smoothed at low speed (STEER_SMOOTH_TAU_S), starting from the wheel with every request
        tau = float(np.interp(CS.out.vEgoRaw * 3.6, CarControllerParams.STEER_SMOOTH_BP_KPH, CarControllerParams.STEER_SMOOTH_TAU_S))
        if not steer_req:
          self.smoothed_angle = CS.out.steeringAngleDeg
        elif tau > 0:
          self.smoothed_angle += (target - self.smoothed_angle) * 2 * DT_CTRL / (tau + 2 * DT_CTRL)
          target = self.smoothed_angle
        else:
          self.smoothed_angle = target
      apply_angle = apply_steer_angle_limits_vm(target, self.apply_angle_last, CS.out.vEgoRaw,
                                                CS.out.steeringAngleDeg, steer_req, CarControllerParams, self.VM)
      if self.yield_to_driver:
        # Every request starts gently. The first angle asked for is the wheel's own, and
        # the limits above then let it move as at any other time: at 30 km/h that is
        # 2.7 deg per frame, a wheel pulled 37 deg in a quarter of a second if
        # openpilot's target is that far away, as it can be after the driver has steered
        # somewhere else. So for the first second of a request the change per frame is
        # further limited, from nothing up to the usual amount.
        self.request_frames = self.request_frames + 1 if steer_req else 0
        if 0 < self.request_frames < CarControllerParams.STEER_REQUEST_RAMP_FRAMES:
          usual = min(get_max_angle_delta_vm(max(CS.out.vEgoRaw, 1), self.VM, CarControllerParams), CarControllerParams.ANGLE_LIMITS.MAX_ANGLE_RATE)
          allowed = usual * self.request_frames / CarControllerParams.STEER_REQUEST_RAMP_FRAMES
          apply_angle = min(max(apply_angle, self.apply_angle_last - allowed), self.apply_angle_last + allowed)
      self.apply_angle_last = apply_angle

      resend = self.steer_frame_refused
      self.steer_frame_refused = False
      if not resend:
        self.counter = (self.counter + 1) % 16
      self.rate_limit = bydcan.step_rate_limit(self.rate_limit, steer_req, **self.rate_limit_values)
      can_sends.append(bydcan.create_steering_control(self.packer, self.apply_angle_last, steer_req,
                                                             self.counter, self.rate_limit, self.set_me_f))
      if not resend:
        can_sends.append(bydcan.create_lkas_hud(self.packer, steer_req, self.counter, CS.lkas_hud, hud_control, self.song_plus_hud))

    if self.watch_eps_answer:
      # The request and the EPS's answer meet here: this side knows whether STEER_REQ
      # is being sent, CarState has just read what the EPS says it is doing.
      self.unanswered_frames = self.unanswered_frames + 1 if (steer_req and CS.eps_lka_state != EPS_LKA_ACTIVE) else 0
      # The same goes for steering frames asking to steer that panda refused: none of
      # them reaches the EPS. They come back to CarState a step or two after we built
      # them; STEER_REJECTED_LIMIT of them within STEER_REJECTED_WINDOW is the fault.
      # CarState reports this as a temporary steering fault, and openpilot stops asking
      # while one is reported, which by itself would end the fault two steps after it
      # began and start the whole thing over. So once it has happened it stays reported
      # until openpilot is no longer engaged: this engagement gets no second try.
      if not CC.enabled:
        self.request_unanswered = False
        self.unanswered_reported_frames = 0
        self.rejected_frames.clear()
      else:
        self.rejected_frames.extend([self.frame] * CS.steer_requests_rejected)
        while self.rejected_frames and self.frame - self.rejected_frames[0] >= CarControllerParams.STEER_REJECTED_WINDOW:
          self.rejected_frames.popleft()
        if (self.unanswered_frames > CarControllerParams.EPS_UNANSWERED_REQUEST_FRAMES or
            len(self.rejected_frames) >= CarControllerParams.STEER_REJECTED_LIMIT):
          self.request_unanswered = True
      self.unanswered_reported_frames += self.request_unanswered
      CS.lka_request_unanswered = self.request_unanswered
      # A temporary fault ends the engagement through a 3 s soft disable, except when
      # the driver was turning the wheel in the 1.5 s before it: then openpilot only
      # shows a small banner (and nothing at all while he is still turning it) and
      # would stay engaged without steering for as long as he let it. Still engaged
      # after 3.5 s means that happened; a permanent fault ends it at once.
      CS.lka_request_given_up = self.unanswered_reported_frames > CarControllerParams.EPS_UNANSWERED_GIVE_UP_FRAMES

    if self.send_cancel_button and CC.cruiseControl.cancel and self.frame % 10 == 0:
      can_sends.append(bydcan.create_buttons(self.packer, cancel=True))

    new_actuators = actuators.as_builder()
    new_actuators.steeringAngleDeg = float(self.apply_angle_last)

    if self.CP.openpilotLongitudinalControl:
      acc_cmd, accel = self.acc_cmd_replacement(CC, CS, now_nanos)
      if acc_cmd is not None:
        can_sends.append(acc_cmd)
      new_actuators.accel = float(accel)

    self.frame += 1
    return new_actuators, can_sends
