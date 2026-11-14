import copy

from opendbc.car import Bus, structs
from opendbc.can.parser import CANParser
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.byd.values import DBC, BydFlags, CarControllerParams as CCP
from opendbc.car.interfaces import CarStateBase

GearShifter = structs.CarState.GearShifter
ButtonType = structs.CarState.ButtonEvent.Type

# STEERING_TORQUE.LKA_STATE
EPS_LKA_OFF, EPS_LKA_READY, EPS_LKA_ACTIVE, EPS_LKA_ABORTED = 0, 1, 2, 3

# ACC_HUD_ADAS.ACC_STATE_CODE values in which the Song Plus's stock ACC is engaged:
# 3 controlling, 5 controlling but overridden by the driver's gas pedal, 7 holding the
# car at standstill. Anything else, including a code not seen yet, reads as not engaged.
SONG_PLUS_ACC_ENGAGED_STATES = (3, 5, 7)

# BYD gear enum from DRIVE_STATE.GEAR
GEAR_MAP = {
  1: GearShifter.park,
  2: GearShifter.reverse,
  3: GearShifter.neutral,
  4: GearShifter.drive,
}


class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    self.lkas_hud = {}
    self.acc_key_prev = False
    self.acc_key_cancel = False
    self.eps_lka_state = EPS_LKA_OFF
    # steering frames asking the EPS to steer that panda refused, in this update,
    # and the counters of all steering frames it refused
    self.steer_requests_rejected = 0
    self.steer_counters_rejected: list[int] = []
    # set by the CarController, which is the side that knows whether we are asking
    self.lka_request_unanswered = False
    self.lka_request_given_up = False

  def update(self, can_parsers) -> structs.CarState:
    cp = can_parsers[Bus.pt]
    cp_cam = can_parsers[Bus.cam]
    ret = structs.CarState()

    # speed
    if self.CP.flags & BydFlags.FOUR_WHEEL_SPEEDS:
      ws = cp.vl["WHEELSPEED_CLEAN"]
      fl, fr, rl, rr = (ws["WHEELSPEED_FL"], ws["WHEELSPEED_FR"],
                        ws["WHEELSPEED_BL"], ws["WHEELSPEED_BR"])
      ret.wheelSpeeds.fl = fl * CV.KPH_TO_MS
      ret.wheelSpeeds.fr = fr * CV.KPH_TO_MS
      ret.wheelSpeeds.rl = rl * CV.KPH_TO_MS
      ret.wheelSpeeds.rr = rr * CV.KPH_TO_MS
      self.parse_wheel_speeds(ret, fl, fr, rl, rr)
      speed_kph = (fl + fr + rl + rr) / 4.0
    else:
      speed_kph = cp.vl["WHEELSPEED_CLEAN"]["WHEELSPEED_CLEAN"]
      ret.vEgoRaw = speed_kph * CV.KPH_TO_MS
      ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.standstill = speed_kph < 0.1
    ret.vEgoCluster = ret.vEgo

    # steering wheel
    ret.steeringAngleDeg = cp.vl["STEER_MODULE_2"]["STEER_ANGLE_2"]
    ret.steeringTorque = cp.vl["STEERING_TORQUE"]["MAIN_TORQUE"]
    if self.CP.flags & BydFlags.DRIVER_TORQUE_NM:
      # Fitted against this car's logged original: MAIN_TORQUE is the EPS motor
      # torque and reproduces steeringTorqueEps exactly (r2 = 1.000000, scale 1).
      # The Atto 3's unsigned 8-bit DRIVER_EPS_TORQUE cannot: the logged value
      # spans +-377. Driver torque is a separate signal in Nm.
      ret.steeringTorqueEps = cp.vl["STEERING_TORQUE"]["MAIN_TORQUE"]
      # DRIVER_TORQUE despite the recording disagreeing, because the recording's
      # steeringTorque is not a decode of anything: it is exactly zero in 98.17% of
      # samples, where real driver torque almost never is, and no window of any
      # bus-0 address correlates with it above |r| 0.42. Where it is non-zero it
      # tracks DRIVER_TORQUE at r 0.77, so it looks like a control-layer term the
      # original logged here. What proves DRIVER_TORQUE is the driver torque is the
      # 24 Nm threshold reproducing logged steeringPressed at 100.000%.
      ret.steeringTorque = cp.vl["STEERING_TORQUE"]["DRIVER_TORQUE"]
      driver_torque = abs(ret.steeringTorque)
      ret.steeringPressed = self.update_steering_pressed(
        driver_torque > CCP.STEER_DRIVER_OVERRIDE_NM, 5)
      ret.steeringDisengage = driver_torque > CCP.STEER_DRIVER_DISENGAGE_NM
    else:
      ret.steeringTorqueEps = cp.vl["STEER_MODULE_2"]["DRIVER_EPS_TORQUE"]
      ret.steeringPressed = self.update_steering_pressed(
        abs(ret.steeringTorqueEps) > CCP.STEER_DRIVER_OVERRIDE, 5)
      ret.steeringDisengage = abs(ret.steeringTorqueEps) > CCP.STEER_DRIVER_DISENGAGE
    # steeringRateDeg is deliberately left unset, the same as the other angle-control
    # ports with no rate signal (ford, nissan).
    #
    # The car does not send one. Swept every (start, width, signedness) window of all
    # 86 bus-0 addresses against the logged rate over 21,518 samples: the best |r| is
    # 0.47, and it sits on 0x11F's own angle bits, i.e. angle magnitude correlating
    # with rate during a manoeuvre, not a rate signal.
    #
    # It is cheaply derivable - the recording's own rate is exactly a 25 ms angle
    # difference, unfiltered: every logged value is an integer multiple of 4 deg/s,
    # and 0.1 deg / 4 deg/s = 25.0 ms. Reproducing that from 0x11F alone gives
    # r2 0.980 with 60.1% of samples exact and a median error of 0.
    #
    # It stays unset anyway because nothing on this platform reads it: the field is
    # optional in car.capnp, latcontrol_pid only echoes it into its log and this car
    # is angle-controlled, and the rate-based EPS fault avoidance that makes subaru
    # derive it does not apply here. Deriving state no consumer checks just adds
    # something that can rot unnoticed.

    # LKAS_STATE follows the BYD TJA_STATE_* enum: 0=Off, 1=Passive, 2/3=Active, 4=Fault.
    # LKS_PREPARED on the EPS is also 1 at route start before any engagement, so it can't be
    # used directly as a fault flag - the camera-side fault state is the reliable indicator.
    ret.steerFaultTemporary = int(cp_cam.vl["LKAS_HUD_ADAS"]["LKAS_STATE"]) == 4
    if self.CP.flags & BydFlags.EPS_LKA_STATE:
      # The line above cannot fire on this car: the stock camera's LKAS_STATE is 0 in
      # every recorded frame. Without the two checks below openpilot has no way to
      # know the EPS has stopped steering. That happened on 2026-11-14: after the
      # driver held the wheel against the command the EPS aborted, and for the next
      # 6.6 s openpilot stayed engaged, kept sending STEER_REQ with a command up to
      # 39 deg away from the wheel, and the EPS applied no torque at all.
      self.eps_lka_state = int(cp.vl["STEERING_TORQUE"]["LKA_STATE"])
      # Aborted is permanent for the rest of the drive: it was still set 109 s later
      # at the end of the recording and in a frame read some ten minutes after that,
      # with the car parked and nothing being requested, and it was gone (ready,
      # 500 of 500 frames) once the car had been switched off and on again.
      # steerUnavailable disengages at once and tells the driver to restart the car.
      ret.steerFaultPermanent = self.eps_lka_state == EPS_LKA_ABORTED
      # Asked to steer and not doing it (off, or ready but ignoring the request).
      # Never seen. The CarController keeps the count, keeps it reported until
      # openpilot has disengaged, and makes it permanent if that takes too long.
      ret.steerFaultTemporary = ret.steerFaultTemporary or self.lka_request_unanswered
      ret.steerFaultPermanent = ret.steerFaultPermanent or self.lka_request_given_up
      # A steering frame panda refuses to send never reaches the EPS, and pandad hands it
      # back to us on its bus + 0xC0. Counted here, judged by the CarController.
      rejected = can_parsers[Bus.loopback].vl_all["STEERING_MODULE_ADAS"]
      self.steer_requests_rejected = sum(1 for steer_req in rejected["STEER_REQ"] if steer_req)
      self.steer_counters_rejected = [int(counter) for counter in rejected["COUNTER"]]

    # gas / brake
    if self.CP.flags & BydFlags.PEDALS_ALT:
      # Measured against this car's logged original over 698,791 samples:
      # DRIVE_STATE.RAW_THROTTLE > 0 reproduces gasPressed at only 90.4% and
      # DRIVE_STATE.BRAKE_PRESSED at 96.8%. The pedals live elsewhere here:
      # PEDAL.GAS_PEDAL > 0.03 gives 99.895% and IPB.BRAKE_PRESSED 99.974%.
      ret.gasPressed = cp.vl["PEDAL"]["GAS_PEDAL"] > 0.03
      ret.brakePressed = bool(cp.vl["IPB"]["BRAKE_PRESSED"])
    else:
      ret.gasPressed = cp.vl["DRIVE_STATE"]["RAW_THROTTLE"] > 0
      ret.brakePressed = bool(cp.vl["DRIVE_STATE"]["BRAKE_PRESSED"])

    # gear
    ret.gearShifter = GEAR_MAP.get(int(cp.vl["DRIVE_STATE"]["GEAR"]), GearShifter.unknown)

    # blinkers
    ret.leftBlinker = bool(cp.vl["STALKS"]["LEFT_BLINKER"])
    ret.rightBlinker = bool(cp.vl["STALKS"]["RIGHT_BLINKER"])

    # blind spot monitor. Reading BSD_RADAR on a car that does not have it leaves
    # the message permanently un-alive, which holds CANParser's can_valid false and
    # blocks engagement, so it is gated on the platform.
    if not self.CP.flags & BydFlags.NO_BSM:
      ret.leftBlindspot = cp.vl["BSD_RADAR"]["LEFT_APPROACH"] != 0
      ret.rightBlindspot = cp.vl["BSD_RADAR"]["RIGHT_APPROACH"] != 0

    # doors / belt
    ret.doorOpen = any((
      cp.vl["METER_CLUSTER"]["FRONT_LEFT_DOOR"],
      cp.vl["METER_CLUSTER"]["FRONT_RIGHT_DOOR"],
      cp.vl["METER_CLUSTER"]["BACK_LEFT_DOOR"],
      cp.vl["METER_CLUSTER"]["BACK_RIGHT_DOOR"],
    ))
    ret.seatbeltUnlatched = not bool(cp.vl["METER_CLUSTER"]["SEATBELT_DRIVER"])

    # cruise state: ACC messages come from camera bus on Atto 3
    # ACC_STATE: 0=OFF, 2=ACC_ON (available), 3=ACC_ACTIVE (enabled), 5=FORCE_ACCEL, 7=ERROR
    if self.CP.flags & BydFlags.ACC_STATE_ALT:
      # Derived from this car's recording (698,791 samples): ACC_STATE at bits
      # 19-21 is constant 0 here. The engage state is the 2-bit field at bits
      # 10-11 (0 = not engaged, 1 and 2 = engaged, 99.997% agreement) and
      # availability is ACC_CMD bit 47 (99.816% with engaged OR'd in).
      # SET_SPEED only means anything while ACC is available: gating it that way
      # raises agreement from 84.9% to 99.8%.
      #
      # Bits 10-11 turned out to be the follow-distance setting, displayed while the
      # ACC is on (it changes right after the distance buttons). They stay non-zero
      # while the ACC is on its way out: measured in the car on 2026-11-14, the driver
      # pressed the ACC key four times within 1.5 s while the ACC was braking; nothing
      # changed until 1.7 s after the first press (0.2 s after the fourth), then the
      # ACC entered state 4 (exiting, the dash prompts the driver) and kept braking for
      # 2.5 s until he braked, with bits 10-11 still set. The ACC's own state is the
      # high nibble of byte 5. Engaged is "on" AND that state being 3, 5 or 7; over
      # 997 s of engaged driving in the recording this never drops out mid-ACC (it
      # differs twice, by 0.02 s and 0.36 s, both real exits seen earlier).
      # panda's byd.h still uses bits 10-11 alone, so it allows for longer than this.
      acc_on = int(cp_cam.vl["ACC_HUD_ADAS"]["ACC_ENGAGED_STATE"]) != 0
      ret.cruiseState.enabled = acc_on and int(cp_cam.vl["ACC_HUD_ADAS"]["ACC_STATE_CODE"]) in SONG_PLUS_ACC_ENGAGED_STATES
      ret.cruiseState.available = bool(cp_cam.vl["ACC_CMD"]["ACC_AVAILABLE"]) or ret.cruiseState.enabled

      # The ACC key (PCM_BUTTONS.ACC_ON_BTN) toggles the stock ACC. Pressed while the
      # ACC is engaged it means "off", and the driver expects everything to let go at
      # that moment - but the ACC itself may show no change for 1.7 s (above). So a
      # press with the ACC engaged is published as a cancel button, which disengages
      # openpilot in the same cycle.
      # No other press is published. From standby it is the driver turning the ACC on,
      # and car_events disables on both edges of a cancel button: the release of such
      # a press, coming after the ACC has engaged, would switch openpilot off again
      # while the ACC stays on. The test is "engaged" and not "on" (bits 10-11) so
      # that this cannot happen even if those bits were ever set in standby; in the
      # recordings they never are. While the ACC is exiting openpilot has already let
      # go, so a press there needs no event either.
      acc_key = bool(cp.vl["PCM_BUTTONS"]["ACC_ON_BTN"])
      if acc_key and not self.acc_key_prev and ret.cruiseState.enabled:
        self.acc_key_cancel = True
        ret.buttonEvents = [structs.CarState.ButtonEvent(pressed=True, type=ButtonType.cancel)]
      elif not acc_key and self.acc_key_prev and self.acc_key_cancel:
        self.acc_key_cancel = False
        ret.buttonEvents = [structs.CarState.ButtonEvent(pressed=False, type=ButtonType.cancel)]
      self.acc_key_prev = acc_key
      ret.cruiseState.speed = (cp_cam.vl["ACC_HUD_ADAS"]["SET_SPEED"] * CV.KPH_TO_MS
                               if ret.cruiseState.available else 0.0)
      # The logged original's speedCluster equals speed exactly, 100.000% of samples.
      ret.cruiseState.speedCluster = ret.cruiseState.speed
      # cruiseState.standstill is deliberately left False here. ACC_CMD's
      # STANDSTILL_STATE and STANDSTILL_RESUME are BOTH constant 0 across all
      # 698,956 samples of the recording, so the Atto 3 read below would be inert
      # on this car - its 95.5% agreement with the log is only both sides being
      # false. No other ACC_CMD signal tracks it either.
      #
      # What does track it is bit 8 of 0x220, an address neither DBC declares, at
      # 99.998%. Do not adopt it: that is vehicle standstill, which is what the
      # original logged into this field, not "ACC is holding the car at standstill"
      # which is what the field means. The recording never has the car stopped with
      # ACC engaged (0 of 698,956 samples), so the two cannot be separated from it.
      # Publishing vehicle standstill would make openpilot believe ACC is holding at
      # every red light with cruise off, and cruise.py / car_events.py drive the
      # resume logic off this. False is both safe and correct for a lateral-only
      # port where the factory ACC does the stopping and the resuming.
    else:
      ret.cruiseState.speed = cp_cam.vl["ACC_HUD_ADAS"]["SET_SPEED"] * CV.KPH_TO_MS
      acc_state = int(cp_cam.vl["ACC_HUD_ADAS"]["ACC_STATE"])
      ret.cruiseState.available = acc_state in (2, 3, 5)
      ret.cruiseState.enabled = acc_state in (3, 5)
      ret.cruiseState.standstill = bool(cp_cam.vl["ACC_CMD"]["STANDSTILL_STATE"])

    # forward stock LKAS HUD
    self.lkas_hud = copy.copy(cp_cam.vl["LKAS_HUD_ADAS"])

    return ret

  @staticmethod
  def get_can_parsers(CP):
    parsers = {
      Bus.pt: CANParser(DBC[CP.carFingerprint][Bus.pt], [], 0),
      Bus.cam: CANParser(DBC[CP.carFingerprint][Bus.pt], [], 2),
    }
    if CP.flags & BydFlags.EPS_LKA_STATE:
      # Our own steering frames that panda refused to send (pandad adds 0xC0 to the bus
      # number of a rejected frame). Normally there are none, and the few there are come
      # with any counter, so neither their absence (nan: no frequency check) nor their
      # counter may make the parser, and with it canValid, invalid.
      rejected = CANParser(DBC[CP.carFingerprint][Bus.pt], [("STEERING_MODULE_ADAS", float('nan'))], 0xC0)
      rejected.message_states[rejected.dbc.name_to_msg["STEERING_MODULE_ADAS"].address].ignore_counter = True
      parsers[Bus.loopback] = rejected
    return parsers
