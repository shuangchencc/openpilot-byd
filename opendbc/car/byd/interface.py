from opendbc.car import get_safety_config, structs
from opendbc.car.interfaces import ACCEL_MAX, CarInterfaceBase
from opendbc.car.byd.carcontroller import CarController
from opendbc.car.byd.carstate import CarState
from opendbc.car.byd.values import CAR, BydSafetyFlags, SongPlusLongitudinal


class CarInterface(CarInterfaceBase):
  CarState = CarState
  CarController = CarController

  @staticmethod
  def get_pid_accel_limits(CP, current_speed, cruise_speed):
    # Matters only with openpilot longitudinal, which is the Song Plus alone: openpilot
    # may ask for no less than the level allows (see SongPlusLongitudinal), and whatever
    # it asks for above the stock ACC's own request the car never sees.
    return SongPlusLongitudinal.ACCEL_MIN, ACCEL_MAX

  @staticmethod
  def _get_params(ret: structs.CarParams, candidate, fingerprint, car_fw, alpha_long, is_release, docs) -> structs.CarParams:
    ret.brand = "byd"

    ret.safetyConfigs = [get_safety_config(structs.CarParams.SafetyModel.byd)]

    ret.dashcamOnly = True

    ret.steerControlType = structs.CarParams.SteerControlType.angle
    ret.steerActuatorDelay = 0.2
    ret.steerLimitTimer = 0.4

    ret.radarUnavailable = True
    ret.alphaLongitudinalAvailable = False

    # Anchored against raw GPS ground speed on this car: the wheel signal reads
    # 5.8% high, the usual regulatory speedometer bias. 83 steady-state pairs with
    # GPS speedAccuracy < 0.5 m/s give a ratio of 0.94528 (std 0.00455), and the
    # regression is a pure scale -- intercept -0.037 m/s, r2 0.999885, residual
    # std 0.0755 m/s. wheelSpeeds stay as the car reports them; this corrects vEgo.
    if candidate == CAR.BYD_SONG_PLUS_DMI_23:
      ret.wheelSpeedFactor = 0.9453
      # Tell panda this is a Song Plus: road-scaled speed + IPB brake (byd.h).
      ret.safetyConfigs[0].safetyParam |= BydSafetyFlags.SONG_PLUS.value

      # Longitudinal, see SongPlusLongitudinal in values.py. alphaLongitudinalAvailable stays
      # False on purpose: there is no toggle for this in the settings, alpha_long comes
      # from the launcher's one-launch switch alone. The stock ACC still engages and
      # disengages (pcmCruise stays True) and its set speed is openpilot's.
      ret.openpilotLongitudinalControl = alpha_long
      if alpha_long:
        ret.safetyConfigs[0].safetyParam |= SongPlusLongitudinal.PANDA_LEVEL << SongPlusLongitudinal.PANDA_LEVEL_SHIFT
        # the lag at which the car's acceleration followed the stock ACC's own requests best
        ret.longitudinalActuatorDelay = 0.3

    return ret
