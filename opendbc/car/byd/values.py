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

  # Platforms that report driver torque in Nm (BydFlags.DRIVER_TORQUE_NM).

  # |DRIVER_TORQUE| > 24.0 Nm reproduces the logged steeringPressed with

  # 100.000% agreement over 86,468 samples across 12 recorded segments.

  STEER_DRIVER_OVERRIDE_NM = 24.0

  # UNVERIFIED: no oracle for the disengage level, so this keeps the 3x ratio

  # the Atto 3 uses (10 -> 30). Must be validated before any actuation.

  STEER_DRIVER_DISENGAGE_NM = 72.0
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
  # Driver torque is STEERING_TORQUE.DRIVER_TORQUE in Nm and the EPS motor torque
  # is MAIN_TORQUE, instead of the Atto 3's single unsigned DRIVER_EPS_TORQUE.
  DRIVER_TORQUE_NM = 4
  # ACC_HUD_ADAS.ACC_STATE (bits 19-21) is constant 0 on this car. The engage
  # state is a 2-bit field at bits 10-11 and availability is ACC_CMD bit 47.
  ACC_STATE_ALT = 8
  # Pedals: gas comes from PEDAL.GAS_PEDAL and brake from IPB (0x123, absent from
  # the Atto 3 DBC), not from DRIVE_STATE.
  PEDALS_ALT = 16


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
  # Wheelbase is the manufacturer's published figure. steerRatio is openpilot's own
  # online estimate from a recorded drive on this car: paramsd reported median 20.59
  # (mean 20.83, std 0.50, range 20.07-21.70) over 139,788 valid samples, converging
  # up from the 19.5 it was initialised with and never returning to it, with
  # stiffnessFactor ~1.013. paramsd uses the camera's visual yaw, independent of the
  # CAN yaw sensor. The CAN yaw signal itself (0x222 bits 0-11, r2 0.955 against
  # v*angle) cannot pin steerRatio down because its scale factor is unknown and
  # multiplicatively coupled to the ratio.
  BYD_SONG_PLUS_DMI_23 = BydPlatformConfig(
    [BydCarDocs("BYD Song PLUS DM-i 2023")],
    CarSpecs(wheelbase=2.765, steerRatio=20.6),
    {Bus.pt: 'byd_song_plus'},
    flags=BydFlags.FOUR_WHEEL_SPEEDS | BydFlags.NO_BSM | BydFlags.DRIVER_TORQUE_NM | BydFlags.ACC_STATE_ALT | BydFlags.PEDALS_ALT,
  )


# Panda's byd.h hardcodes the vehicle model used for its own angle check
# (slip_factor/steer_ratio/wheelbase from BYD_ATTO_3, "to match openpilot"), so the
# controller must limit with the SAME geometry or panda rejects its commands. Until
# byd.h takes the geometry from a safety param, every BYD platform is limited by
# Atto 3 geometry.
#
# Measured consequence for BYD_SONG_PLUS_DMI_23 (steerRatio 19.5 vs 14.8): its own
# geometry would allow 1.333x more steering angle, e.g. at 90 km/h 24.46 deg and
# 0.489 deg/frame versus the 18.35 deg and 0.367 deg/frame panda enforces. That is
# the conservative direction - the intended lateral acceleration is never exceeded -
# so this is a capability limit, not a safety hazard. Changing only the controller
# side would instead desynchronise the two and get commands rejected.
SAFETY_VM_PLATFORM = "BYD_ATTO_3"


FW_QUERY_CONFIG = FwQueryConfig(
  requests=[],
  fw_version_regex=b"",
)


DBC = CAR.create_dbc_map()
