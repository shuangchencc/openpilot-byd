from opendbc.car import get_safety_config, structs
from opendbc.car.interfaces import CarInterfaceBase
from opendbc.car.byd.carcontroller import CarController
from opendbc.car.byd.carstate import CarState
from opendbc.car.byd.values import CAR, BydSafetyFlags


class CarInterface(CarInterfaceBase):
  CarState = CarState
  CarController = CarController

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

    return ret
