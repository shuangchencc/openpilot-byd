from collections import deque

from opendbc.can.packer import CANPacker
from opendbc.car import Bus
from opendbc.car.lateral import apply_steer_angle_limits_vm, get_max_angle_delta_vm, get_max_angle_vm
from opendbc.car.interfaces import CarControllerBase
from opendbc.car.byd import bydcan
from opendbc.car.byd.carstate import EPS_LKA_ACTIVE
from opendbc.car.byd.values import BydFlags, CarControllerParams, SAFETY_VM_PLATFORM
from opendbc.car.vehicle_model import VehicleModel


def get_safety_CP():
  # Must stay in step with panda's byd.h; see SAFETY_VM_PLATFORM in values.py.
  from opendbc.car.byd.interface import CarInterface
  return CarInterface.get_non_essential_params(SAFETY_VM_PLATFORM)


class CarController(CarControllerBase):
  def __init__(self, dbc_names, CP):
    super().__init__(dbc_names, CP)
    self.packer = CANPacker(dbc_names[Bus.pt])
    self.apply_angle_last = 0.0

    # Vehicle model used for lateral limiting
    self.VM = VehicleModel(get_safety_CP())

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
    # the next request starts from where the wheel then is.
    steer_req = CC.latActive
    if self.yield_to_driver:
      if CS.out.steeringPressed:
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

    if self.frame % 2:
      apply_angle = apply_steer_angle_limits_vm(actuators.steeringAngleDeg, self.apply_angle_last, CS.out.vEgoRaw,
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

      cntr = (self.frame // 2) % 16
      self.rate_limit = bydcan.step_rate_limit(self.rate_limit, steer_req, **self.rate_limit_values)
      can_sends.append(bydcan.create_steering_control(self.packer, self.apply_angle_last, steer_req,
                                                             cntr, self.rate_limit, self.set_me_f))
      can_sends.append(bydcan.create_lkas_hud(self.packer, steer_req, cntr, CS.lkas_hud, hud_control, self.song_plus_hud))

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

    self.frame += 1
    return new_actuators, can_sends
