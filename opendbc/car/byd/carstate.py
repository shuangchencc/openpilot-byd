import copy

from opendbc.car import Bus, structs
from opendbc.can.parser import CANParser
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.byd.values import DBC, BydFlags, CarControllerParams as CCP
from opendbc.car.interfaces import CarStateBase

GearShifter = structs.CarState.GearShifter

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
      # original logged here. What proves DRIVER_TORQUE is the driver torque is that
      # |DRIVER_TORQUE| > 24 reproduces the logged steeringPressed at 100.000%.
      ret.steeringTorque = cp.vl["STEERING_TORQUE"]["DRIVER_TORQUE"]
      # steeringPressed is "a hand is on the wheel" here, not that takeover level:
      # measured around the sensor's zero, which is not 0. See values.py.
      ret.steeringPressed = self.update_steering_pressed(
        abs(ret.steeringTorque - CCP.DRIVER_TORQUE_ZERO_NM) > CCP.STEER_DRIVER_OVERRIDE_NM, 5)
      ret.steeringDisengage = abs(ret.steeringTorque) > CCP.STEER_DRIVER_DISENGAGE_NM
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
      ret.cruiseState.enabled = int(cp_cam.vl["ACC_HUD_ADAS"]["ACC_ENGAGED_STATE"]) != 0
      ret.cruiseState.available = bool(cp_cam.vl["ACC_CMD"]["ACC_AVAILABLE"]) or ret.cruiseState.enabled
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
    return {
      Bus.pt: CANParser(DBC[CP.carFingerprint][Bus.pt], [], 0),
      Bus.cam: CANParser(DBC[CP.carFingerprint][Bus.pt], [], 2),
    }
