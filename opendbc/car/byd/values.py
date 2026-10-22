from dataclasses import dataclass, field
from enum import IntFlag

from opendbc.car import ACCELERATION_DUE_TO_GRAVITY, Bus, CarSpecs, DbcDict, PlatformConfig, Platforms
from opendbc.car.lateral import AngleSteeringLimitsVM, ISO_LATERAL_ACCEL
from opendbc.car.docs_definitions import CarDocs, CarHarness, CarParts
from opendbc.car.fw_query_definitions import FwQueryConfig


# Add extra tolerance for average banked road since safety doesn't have the roll
AVERAGE_ROAD_ROLL = 0.06  # ~3.4 degrees, 6% superelevation. higher actual roll lowers lateral acceleration


class CarControllerParams:
  STEER_STEP = 2  # Angle command is sent at 50 Hz

  # On a fault STEERING_TORQUE.LKS_PREPARED goes from 0 to 1.
  # STEERING_TORQUE.MAIN_TORQUE is saturated at -300 for around 900ms,
  # while the wheel sits 15-26 deg past the commanded TARGET_ANGLE.
  ANGLE_LIMITS: AngleSteeringLimitsVM = AngleSteeringLimitsVM(
    390,  # deg
    # Vehicle model angle limits
    # Add extra tolerance for average banked road since safety doesn't have the roll
    MAX_LATERAL_ACCEL=ISO_LATERAL_ACCEL + (ACCELERATION_DUE_TO_GRAVITY * AVERAGE_ROAD_ROLL),  # ~3.6 m/s^2
    MAX_LATERAL_JERK=3.0 + (ACCELERATION_DUE_TO_GRAVITY * AVERAGE_ROAD_ROLL),  # ~3.6 m/s^3

    # limit angle rate to both prevent a fault and for low speed comfort
    MAX_ANGLE_RATE=5,  # deg/20ms frame
  )

  STEER_DRIVER_OVERRIDE = 10   # EPS torque threshold for soft override
  STEER_DRIVER_DISENGAGE = 30  # EPS torque threshold for hard disengage


@dataclass
class BydCarDocs(CarDocs):
  package: str = "All"
  car_parts: CarParts = field(default_factory=CarParts.common([CarHarness.custom]))


class BydFlags(IntFlag):
  # 0x1F0 carries four 12-bit per-wheel speeds instead of one clean speed.
  FOUR_WHEEL_SPEEDS = 1
  # No blind spot radar on 0x418. Without this the BSD_RADAR read below never
  # goes alive and CANParser keeps can_valid false forever.
  NO_BSM = 2


@dataclass
class BydPlatformConfig(PlatformConfig):
  dbc_dict: DbcDict = field(default_factory=lambda: {
    Bus.pt: 'byd_atto3',
  })


class CAR(Platforms):
  BYD_ATTO_3 = BydPlatformConfig(
    [BydCarDocs("BYD Atto 3 2022-25")],
    CarSpecs(wheelbase=2.72, steerRatio=14.8),
  )
  # Wheelbase is the manufacturer's published figure. steerRatio is NOT yet
  # measured on this car and only matters once lateral control is enabled;
  # it must be validated before any actuation work.
  BYD_SONG_PLUS_DMI_23 = BydPlatformConfig(
    [BydCarDocs("BYD Song PLUS DM-i 2023")],
    CarSpecs(wheelbase=2.765, steerRatio=19.5),
    {Bus.pt: 'byd_song_plus'},
    flags=BydFlags.FOUR_WHEEL_SPEEDS | BydFlags.NO_BSM,
  )


FW_QUERY_CONFIG = FwQueryConfig(
  requests=[],
  fw_version_regex=b"",
)


DBC = CAR.create_dbc_map()
