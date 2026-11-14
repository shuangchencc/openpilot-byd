from opendbc.can.packer import CANPacker
from opendbc.car import Bus
from opendbc.car.lateral import apply_steer_angle_limits_vm
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

    # See BydFlags.LKAS_HUD_ALT
    self.song_plus_hud = bool(CP.flags & BydFlags.LKAS_HUD_ALT)

    # See BydFlags.EPS_LKA_STATE
    self.watch_eps_answer = bool(CP.flags & BydFlags.EPS_LKA_STATE)
    self.unanswered_frames = 0
    self.request_unanswered = False
    self.unanswered_reported_frames = 0

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

    if self.frame % 2:
      self.apply_angle_last = apply_steer_angle_limits_vm(actuators.steeringAngleDeg, self.apply_angle_last, CS.out.vEgoRaw,
                                                          CS.out.steeringAngleDeg, steer_req, CarControllerParams, self.VM)

      cntr = (self.frame // 2) % 16
      self.rate_limit = bydcan.step_rate_limit(self.rate_limit, steer_req, **self.rate_limit_values)
      can_sends.append(bydcan.create_steering_control(self.packer, self.apply_angle_last, steer_req,
                                                             cntr, self.rate_limit, self.set_me_f))
      can_sends.append(bydcan.create_lkas_hud(self.packer, steer_req, cntr, CS.lkas_hud, hud_control, self.song_plus_hud))

    if self.watch_eps_answer:
      # The request and the EPS's answer meet here: this side knows whether STEER_REQ
      # is being sent, CarState has just read what the EPS says it is doing.
      self.unanswered_frames = self.unanswered_frames + 1 if (steer_req and CS.eps_lka_state != EPS_LKA_ACTIVE) else 0
      # CarState reports this as a temporary steering fault, and openpilot stops asking
      # while one is reported, which by itself would end the fault two steps after it
      # began and start the whole thing over. So once it has happened it stays reported
      # until openpilot is no longer engaged: this engagement gets no second try.
      if not CC.enabled:
        self.request_unanswered = False
        self.unanswered_reported_frames = 0
      elif self.unanswered_frames > CarControllerParams.EPS_UNANSWERED_REQUEST_FRAMES:
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
