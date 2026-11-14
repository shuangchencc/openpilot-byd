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

  # Platforms with BydFlags.EPS_LKA_STATE: how long we may ask the EPS to steer
  # without it reporting that it is, before that counts as a fault. The EPS answered
  # all 20 recorded requests (18 of the vendor's stack, 2 of ours) within 46 ms, so
  # 0.5 s is ten times the worst seen.
  EPS_UNANSWERED_REQUEST_FRAMES = 50  # at 100 Hz
  # Platforms with BydFlags.YIELD_TO_DRIVER: how long after the driver has stopped
  # turning the wheel (steeringPressed) the EPS is asked to steer again. Long enough
  # that a torque hovering at the threshold is one withdrawal and not a string of them.
  STEER_YIELD_HOLD_FRAMES = 50  # at 100 Hz
  # ... and over how many steering frames, from the first one that asks the EPS to
  # steer, the allowed change of the commanded angle per frame grows from nothing
  # to what the limits allow. That goes for every request, the first of an
  # engagement included.
  STEER_REQUEST_RAMP_FRAMES = 50  # at 50 Hz

  # ... and how long that fault may be reported with openpilot still engaged before it
  # is made permanent. Longer than selfdrived's 3 s soft disable, so that only the
  # cases in which there was none get here.
  EPS_UNANSWERED_GIVE_UP_FRAMES = 350


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
  # It does not jump between the two: in all 349,404 frames the stack that made the
  # recording sent, it moves by 16 per frame, up while STEER_REQ is set and down
  # while it is not. With that ramp the EPS left its steering state within 41 ms of
  # STEER_REQ being dropped (18 of 18 times); the one time it was sent 300 at once,
  # by us, it kept steering for another 240 ms.
  # Evidence: migration-audit/byd-safety-verification-20261114/out/steer-frame-replay-*.txt
  TX_CONSTANTS_ALT = 32
  # Pedals: gas comes from PEDAL.GAS_PEDAL and brake from IPB (0x123, absent from
  # the Atto 3 DBC), not from DRIVE_STATE.
  PEDALS_ALT = 16
  # Do not transmit the cruise-cancel button (PCM_BUTTONS, 0x3B0) on this car.
  # The port sends it on bus 0, which is where the car's own body controller sends
  # that message from. The consumer, the ADAS controller that runs the stock ACC,
  # is on bus 2: in a recorded drive the vendor's stack relayed every one of the
  # car's 139,878 frames from bus 0 to bus 2 itself, with its panda blocking the
  # direct forward. So a cancel on bus 0 would not reach the ACC and would sit on
  # the body bus next to the car's own frames of the same address.
  # Inferred from that recording, not tested in the car. Until the bus 2 route has
  # been tried there, the port stays out of it: when openpilot disengages, the
  # stock ACC keeps going and the driver cancels it.
  # Evidence: migration-audit/byd-safety-verification-20261114/RESULT.txt, section 8.
  NO_CANCEL_BUTTON = 64
  # The EPS reports what it is doing with our steering command in
  # STEERING_TORQUE.LKA_STATE (see the DBC), and that is this car's only steering
  # fault indication: the camera-side LKAS_STATE the Atto 3 path reads is constant 0
  # here, and the EPS's TORQUE_FAILED / TORQUE_TEMP_FAILED bits have never been seen
  # set, not even when it aborted.
  # Evidence: migration-audit/byd-safety-verification-20261114/out/eps-request-answer-*.txt
  EPS_LKA_STATE = 128
  # Do not ask the EPS to steer while the driver is turning the wheel.
  # Asked to hold an angle, this EPS opposes the driver with its whole motor torque.
  # On 2026-11-14 the driver turned the wheel with openpilot engaged (torque 36-41 on
  # the DBC's scale): the motor torque reached its limit of 499 and stayed there for
  # 0.68 s, then the EPS aborted (LKA_STATE 3) and would not steer again until the car
  # had been switched off and on. STEER_REQ had been kept up for 1.05 s after
  # steeringPressed.
  # The stack that made the 116 min recording never let it get that far: it sent
  # STEER_REQ 0 in every frame in which the driver's torque was above 28 and
  # STEER_REQ 1 in every other, without exception in the 49,834 frames it was engaged
  # for. That was one episode, torque up to 41.7: the EPS let go within 41 ms each
  # time, its motor torque never passed 377, and it took the next request within
  # 41 ms with the driver's torque still at 22-27.
  # We withdraw a little earlier (steeringPressed: above 24 for 60 ms) and come back
  # later (STEER_YIELD_HOLD_FRAMES after it ends), so that the wheel is not taken
  # again while the driver is still turning it, which in that episode happened five
  # times in 1.5 s. And we come back gently (STEER_REQUEST_RAMP_FRAMES): the wheel
  # may by then be far from where openpilot wants it.
  # Evidence: migration-audit/byd-safety-verification-20261114/out/yield-replay-*.txt
  YIELD_TO_DRIVER = 512
  # LKAS_HUD_ADAS carries the values this car's cluster is known to have been sent
  # while steering, not the Atto 3's. See bydcan.create_lkas_hud.
  # Evidence: migration-audit/byd-safety-verification-20261114/out/hud-replay-*.txt
  LKAS_HUD_ALT = 256


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
    flags=BydFlags.FOUR_WHEEL_SPEEDS | BydFlags.NO_BSM | BydFlags.DRIVER_TORQUE_NM | BydFlags.ACC_STATE_ALT | BydFlags.TX_CONSTANTS_ALT | BydFlags.PEDALS_ALT |
          BydFlags.NO_CANCEL_BUTTON | BydFlags.EPS_LKA_STATE | BydFlags.LKAS_HUD_ALT | BydFlags.YIELD_TO_DRIVER,
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
