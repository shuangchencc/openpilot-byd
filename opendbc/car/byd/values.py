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

  # Platforms that report driver torque on STEERING_TORQUE.DRIVER_TORQUE
  # (BydFlags.DRIVER_TORQUE_NM).
  #
  # Units. The DBC labels the signal "Nm" with factor 0.1, but one unit is about
  # 0.1 Nm: dry-park steering peaks at 27 units on this car, which is 2.7 Nm and
  # cannot be 27. Every *_NM constant here is in DBC units; the names are kept.
  #
  # steeringPressed on this platform means "a hand is on the wheel". That is what
  # its consumers need: the wheel-touch driver monitoring policy (the only driver
  # monitoring this car has, with no cabin camera), the lane-change nudge and
  # selfdrived. Nothing in the BYD carcontroller or in byd.h reads it.
  #
  # It is deliberately NOT the vendor's steeringPressed. |DRIVER_TORQUE| > 24.0
  # reproduces that one with 100.000% agreement over 86,468 samples across 12
  # recorded segments, which is what identified DRIVER_TORQUE as the driver's
  # torque. But 24 (about 2.4 Nm) is a takeover level: replayed over a 116 min
  # drive it leaves the wheel-touch policy past its 25 s red alert 30-40 times an
  # hour, hands on or not, and a deliberate firm grab on 2026-11-12 peaked at 16.
  #
  # Measured 2026-11-12/13, parked, labelled capture at 100 Hz:
  #   hands off                  -1.33 +-0.06, the same at -4.1 and -8.0 deg
  #   two hands resting          +2.64 mean (-3.5 .. +6.4)
  #   one hand at 6 o'clock      +3.41 mean ( 0.0 .. +5.8)
  #   light tug left/right       -10.0 .. +11.5
  # and from a recorded drive, the level the signal sits at when it goes flat
  # while moving: about -1.1.
  #
  # The sensor's zero is not 0, so a threshold on the raw value is lopsided: the
  # same hand weight reads about -5.4 one way and +2.6 the other. Hence a zero
  # and a threshold around it. |x - ZERO| > 3.0 is true 60-90% of the time with a
  # hand resting and never with hands off. The zero is the moving one, not a
  # parked reading: parked, column friction can hold a residual down to -3.4
  # after dry steering, which still falls inside the band (raw -4.1 .. +1.9).
  # The zero is fixed, not adaptive, on purpose: a zero that follows the signal
  # learns a steady hand and then reads that hand's removal as a touch.
  #
  # NOT yet validated: what the sensor reads hands-off while the EPS is steering.
  # That needs a closed-course run (accept: no detection in 60 s hands-off and
  # |x - ZERO| <= 2). Until then these two values are provisional.
  # Evidence: migration-audit/steer-torque-threshold-20261112/RESULT.txt.
  DRIVER_TORQUE_ZERO_NM = -1.1
  STEER_DRIVER_OVERRIDE_NM = 3.0

  # Deliberately above the reachable range rather than a tuned trip level.
  # Measured over 349,699 EPS frames of a full route: |DRIVER_TORQUE| peaks at
  # 41.7 Nm (p99.9 23.2), and the most held for 5 samples while lateral control
  # was active was 34.8 Nm - so any threshold at or below that would have
  # disengaged on a drive where the stock system did not.
  #
  # It has to stay unreachable until panda implements the matching check. byd.h
  # never assigns steering_disengage (tesla.h is the only mode that does), while
  # test_models asserts CarState.steeringDisengage equals panda's
  # steering_disengage_prev. Both sides are permanently false today and therefore
  # agree; making this reachable without the byd.h half would break that contract.
  # Lowering it is a two-sided change and needs a measured hard-grab level.
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
  # Steering-command constants differ from the Atto 3's. Measured from 71,296
  # frames this car's EPS demonstrably accepted: SET_ME_F is 0 (not 0xF) on every
  # frame, and ANGLE_RATE_LIMIT is +-500 engaged / +-300 idle (not +-299 / 0).
  TX_CONSTANTS_ALT = 32
  # Pedals: gas comes from PEDAL.GAS_PEDAL and brake from IPB (0x123, absent from
  # the Atto 3 DBC), not from DRIVE_STATE.
  PEDALS_ALT = 16


class BydSafetyFlags(IntFlag):
  # Panda safety param; MUST match BYD_PARAM_SONG_PLUS in opendbc/safety/modes/byd.h.
  # Selects the Song Plus wheel-speed scale (road) and brake source (IPB) in panda.
  # Steering geometry is deliberately NOT gated on it (see byd.h / SAFETY_VM_PLATFORM).
  SONG_PLUS = 1


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
    flags=BydFlags.FOUR_WHEEL_SPEEDS | BydFlags.NO_BSM | BydFlags.DRIVER_TORQUE_NM | BydFlags.ACC_STATE_ALT | BydFlags.TX_CONSTANTS_ALT | BydFlags.PEDALS_ALT,
  )


# Panda's byd.h uses the BYD_ATTO_3 vehicle model for its own angle check on every
# BYD platform, so the controller must limit with the SAME geometry or panda rejects
# its commands.
#
# For BYD_SONG_PLUS_DMI_23 this is a settled choice, not a stopgap. Its CarSpecs
# steerRatio is 20.6, paramsd's in-model estimate on this car (median 20.59 over
# 139,788 samples, converged up from 19.5 and never back). A model-free kinematic fit
# against raw GPS yaw instead brackets the *geometric* ratio at 13.2-17.6 on strong
# low-speed turns. Those are different quantities - paramsd reports what makes
# openpilot's own vehicle model reproduce the observed yaw, including whatever its
# slip term under-models on a heavier car - so neither measurement confirms the other,
# and a safety backstop should not move on an unresolved number.
#
# Staying on Atto 3 geometry allows ~1.39x less steering angle than 20.6 would. That
# is the conservative direction, so it is a capability limit, not a safety hazard.
# Changing only this side would desynchronise the two and get commands rejected.
SAFETY_VM_PLATFORM = "BYD_ATTO_3"


FW_QUERY_CONFIG = FwQueryConfig(
  requests=[],
  fw_version_regex=b"",
)


DBC = CAR.create_dbc_map()
