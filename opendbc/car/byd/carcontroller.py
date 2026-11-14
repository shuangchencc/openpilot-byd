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

    # Steering-command constants this car's EPS expects, measured from accepted frames
    if CP.flags & BydFlags.TX_CONSTANTS_ALT:
      self.tx_constants = {'rate_limit_engaged': 500, 'rate_limit_idle': 300, 'set_me_f': 0}
    else:
      self.tx_constants = {'rate_limit_engaged': 299, 'rate_limit_idle': 0, 'set_me_f': 0xF}

    # See BydFlags.NO_CANCEL_BUTTON
    self.send_cancel_button = not (CP.flags & BydFlags.NO_CANCEL_BUTTON)

    # See BydFlags.EPS_LKA_STATE
    self.watch_eps_answer = bool(CP.flags & BydFlags.EPS_LKA_STATE)
    self.unanswered_frames = 0
    self.request_unanswered = False
    self.unanswered_reported_frames = 0

  def update(self, CC, CS, now_nanos):
    can_sends = []
    actuators = CC.actuators
    hud_control = CC.hudControl

    if self.frame % 2:
      self.apply_angle_last = apply_steer_angle_limits_vm(actuators.steeringAngleDeg, self.apply_angle_last, CS.out.vEgoRaw,
                                                          CS.out.steeringAngleDeg, CC.latActive, CarControllerParams, self.VM)

      cntr = (self.frame // 2) % 16
      can_sends.append(bydcan.create_steering_control(self.packer, self.apply_angle_last, CC.latActive,
                                                             cntr, **self.tx_constants))
      can_sends.append(bydcan.create_lkas_hud(self.packer, CC.latActive, cntr, CS.lkas_hud, hud_control))

    if self.watch_eps_answer:
      # The request and the EPS's answer meet here: this side knows whether STEER_REQ
      # is being sent, CarState has just read what the EPS says it is doing.
      steer_req = CC.latActive
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
