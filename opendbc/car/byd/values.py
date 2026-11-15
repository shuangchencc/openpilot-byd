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
  # ... and how many steering frames asking to steer panda may refuse within a window
  # before that counts as the same fault. A refused frame never reaches the EPS and
  # the stock camera's are behind the relay, so while panda refuses frame after frame
  # the EPS gets no steering frame at all. A single one, or two, is what a brake press
  # costs: panda withdraws permission on the brake frame itself, before openpilot can
  # stop asking (one in the car on 2026-11-14, two in a closed-loop replay).
  STEER_REJECTED_LIMIT = 3
  STEER_REJECTED_WINDOW = 50  # steps at 100 Hz
  # Platforms with BydFlags.YIELD_TO_DRIVER: how long after the driver has stopped
  # turning the wheel (steeringPressed) the EPS is asked to steer again. Long enough
  # that a torque hovering at the threshold is one withdrawal and not a string of them.
  STEER_YIELD_HOLD_FRAMES = 50  # at 100 Hz
  # ... and over how many steering frames, from the first one that asks the EPS to
  # steer, the allowed change of the commanded angle per frame grows from nothing
  # to what the limits allow. That goes for every request, the first of an
  # engagement included.
  STEER_REQUEST_RAMP_FRAMES = 50  # at 50 Hz
  # ... and how far inside the lateral acceleration limit's angle the wheel has to be
  # for a request to start. The margin is for panda measuring the speed, and with it
  # the limit, a little differently (it allows for 1 m/s, 8 % of the angle at 90 km/h).
  STEER_REQUEST_START_ANGLE_RATIO = 0.9
  # ... except that while openpilot is changing lanes (CC.leftBlinker / CC.rightBlinker, from the moment it
  # shows the lane change until the driver's blinker goes off), a torque toward that side below this is the
  # push that starts the lane change, not the driver taking the wheel: the request stays up. openpilot
  # starts a lane change on steeringPressed (above 24) in the blinker's direction, the same signal we
  # withdraw on, so without this every lane change began with the EPS letting go for 0.8-1.1 s and then
  # one second of gentle start, the car meanwhile wherever the driver's push had put it.
  # The owner's 16 lane changes with openpilot on 2026-11-15 (route 0000002e, 49-95 km/h) were pushes
  # peaking at 26.2-32.5, above 24 for 0.10-0.18 s, the EPS motor torque at 301-352 (its limit is 499);
  # every one was a withdrawal of 0.58-1.2 s. The stack that made the 116 min recording withdrew above 28
  # (see BydFlags.YIELD_TO_DRIVER), which half of those pushes reached. The one abort seen took 36-41
  # held for 0.68 s. So the push may go up to 35, and only for its first half second: a push held longer
  # is the driver steering, and is withdrawn from as any other.
  # Evidence: migration-audit/lane-change-sim-20261115/RESULT.txt, migration-audit/free-drive-20261115
  STEER_YIELD_LANE_CHANGE_PUSH_MAX = 35.0   # driver torque, the DBC's scale (as STEER_DRIVER_OVERRIDE_NM)
  STEER_YIELD_LANE_CHANGE_PUSH_FRAMES = 50  # at 100 Hz, counted while steeringPressed

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
  # The flag also makes the port watch the steering frames panda refuses to send,
  # which the EPS would otherwise only show as not steering, half a second later.
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
  # And the steering geometry panda checks angles with (see byd.h / SONG_PLUS_SAFETY_STEER_RATIO).
  SONG_PLUS = 1


class SongPlusLongitudinal:
  """Song Plus, when openpilot does the longitudinal control (CarParams.openpilotLongitudinalControl).

  The stock ACC stays what engages and disengages (pcmCruise), and it keeps computing and
  sending its own acceleration request, ACC_CMD. openpilot can only make the car ask for
  less: when it wants a lower acceleration than the stock ACC has just asked for, the
  CarController sends a copy of that stock frame with the request lowered, in the place of
  the next stock frame. When it does not, or cannot, the stock frames go to the car as they
  are. panda checks every such frame against the stock one byte by byte; the rule, and why
  it is "the next frame", is at the top of opendbc/safety/modes/byd.h.
  Facts and design: migration-audit/longitudinal-l0-20261114/RESULT.txt and
  migration-audit/longitudinal-l1-20261114/DESIGN.txt.
  """
  # ACC_CMD byte 0: request [m/s^2] = (raw - REQUEST_ZERO) * REQUEST_SCALE. On this car the
  # stock ACC's own requests against the acceleration of the wheel speed gave a slope of
  # 0.89 at r 0.96 (29 s, -1.05 .. +0.60 m/s^2), 0.30 s behind the request.
  REQUEST_SCALE = 0.05
  REQUEST_ZERO = 100
  # What a stock frame looks like while the stock ACC is controlling: bit 39 clear and
  # byte 5 exactly this (48,014 of 48,101 recorded frames). Nothing else is copied.
  CONTROLLING_BYTE_5 = 0xD8
  CONTROLLING_STATE_CODE = 3   # ACC_HUD_ADAS.ACC_STATE_CODE
  # No frame while the stock ACC shows one of its warning bits (ACC_HUD_ADAS bits 16, 17,
  # 23), nor for this long after: panda holds off for 2 s from the last frame that had one,
  # and we wait a little longer than panda so as not to send what it will refuse.
  WARNING_HOLD_NANOS = 2_100_000_000

  # The lowest request per level, raw; MUST match BYD_LONG_MIN_REQUEST in byd.h
  # (-1.0, -2.0 and -3.5 m/s^2).
  MIN_REQUEST = {1: 80, 2: 60, 3: 30}
  # The level the CarController keeps to. It is raised one step at a time, each after the
  # one before has been driven on a closed course: level 1 there on 2026-11-15 (L4, two
  # drives, migration-audit/longitudinal-l1-20261114/l4/RESULT.txt); level 2 is L5.
  LEVEL = 2
  # The level panda is given in the safety param (bits 1-2). 0: panda does not know
  # ACC_CMD as something openpilot may send and refuses every one of these frames, so
  # nothing reaches the car and the refused frames come back in the log - which is how the
  # first runs in the car are meant to go. To be set to LEVEL together with flashing a
  # panda firmware that has the rule; a firmware without it ignores the bits.
  PANDA_LEVEL = 2
  PANDA_LEVEL_SHIFT = 1

  ACCEL_MIN = (MIN_REQUEST[LEVEL] - REQUEST_ZERO) * REQUEST_SCALE   # m/s^2

  # Raising: openpilot may also ask for a little more than the stock ACC, on a free road only - the
  # stock ACC not braking, neither its radar nor openpilot seeing anything ahead for two seconds,
  # the car moving and below the set speed - by at most RAISE_MAX_ABOVE_STOCK and never above
  # RAISE_MAX_REQUEST, climbing by RAISE_MAX_STEP per frame over what the car got in the frame
  # before. byd.h checks all of it (byd_acc_cmd_raise_ok) except what openpilot sees, which the
  # panda cannot know. Design: migration-audit/longitudinal-l6-20261114/DESIGN.txt.
  # RAISE: the CarController raises, and panda is told it may (safety param bit 3). To be switched
  # on only with a panda firmware that has the rule; one without it refuses every raised frame, and
  # the stock frame goes to the car instead.
  RAISE = False
  PANDA_RAISE_BIT = 8
  # MUST match byd.h: BYD_RAISE_MAX_ABOVE_STOCK, BYD_RAISE_MAX_REQUEST, BYD_RAISE_MAX_STEP, BYD_RAISE_MIN_SPEED
  RAISE_MAX_ABOVE_STOCK = 10      # counts: +0.5 m/s^2 over the stock request
  RAISE_MAX_REQUEST = 124         # +1.2 m/s^2
  RAISE_MAX_STEP = 1              # counts over the request the car got in the frame before: 2.5 m/s^3
  RAISE_MIN_SPEED = 3.0           # m/s of road speed
  # How far above the stock request the CarController itself goes: no more than panda allows, and
  # less while raising is first driven (the closed-course plan starts at +0.2 m/s^2, 4 counts)
  RAISE_ABOVE_STOCK_USED = RAISE_MAX_ABOVE_STOCK
  # panda holds the lowest of its last six wheel speeds (120 ms) against RAISE_MIN_SPEED and the
  # highest against the set speed; we only have the newest, so we keep these margins
  RAISE_MIN_SPEED_MARGIN = 0.5            # m/s
  RAISE_SET_SPEED_MARGIN = 1.0 / 3.6      # m/s (1 km/h), on the speedometer's scale
  # panda raises nothing for 2 s after its radar last showed something ahead; we wait a little
  # longer, and as long after openpilot itself last saw a lead
  LEAD_HOLD_NANOS = 2_100_000_000
  # After panda refused a frame of ours (it comes back on bus 0xC0), nothing is raised for this
  # long: what the car got in that slot was not ours. It also keeps the refusals down to about
  # one a second should RAISE ever be on with a panda that cannot raise.
  RAISE_AFTER_REFUSAL_NANOS = 1_000_000_000


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


# Panda's byd.h checks steering angles with a vehicle model, and the controller must limit with the same one or
# panda rejects its commands (carcontroller.get_safety_CP). The Atto 3 uses its own CarSpecs.
SAFETY_VM_PLATFORM = "BYD_ATTO_3"
#
# The Song PLUS uses its own geometry, measured model-free on 2026-11-15 with the device's gyroscope: yaw rate against
# speed x wheel angle over route 0000002e (78 min, 38-109 km/h), migration-audit/byd-safety-verification-20261114/
# scripts/steer_ratio_imu.py. Apparent steering ratio (wheel angle over road wheel angle that the turn implies):
#   10-30 km/h 17.84 (90% over segments 17.48-18.19, r2 0.993), 30-50 km/h 20.21, 50-70 km/h 25.45, 70-90 km/h 27.54
# - the ratio itself at low speed, and an understeer that grows with speed, as it does. (CarSpecs' 20.6 above is
# paramsd's estimate, the fit of openpilot's own model, not this.)
# Panda and controller take steerRatio 17.2 and slip factor -0.0008 (the front tyre stiffness solved for it), which
# turn LESS than the car at every measured speed, so that the angle the limit allows never means more than the
# 3.6 m/s^2 of lateral acceleration it stands for. In real terms the limit is 3.56 m/s^2 at 20 km/h, 3.37 at 40,
# 2.97 at 60 and 3.14 at 80; with the Atto 3's geometry, used for the Song PLUS until then, it was 2.99, 2.79,
# 2.41 and 2.48, which the owner felt as not enough steering in tight curves. Above 90 km/h nothing is measured
# (no curves were driven there); the model's 27.8 at 100 and 32.5 at 120 km/h assume the understeer goes on growing
# at least that much, which it did up to 90.
SONG_PLUS_SAFETY_STEER_RATIO = 17.2
SONG_PLUS_SAFETY_SLIP_FACTOR = -0.0008


FW_QUERY_CONFIG = FwQueryConfig(
  requests=[],
  fw_version_regex=b"",
)


DBC = CAR.create_dbc_map()
