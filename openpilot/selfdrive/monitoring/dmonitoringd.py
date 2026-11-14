#!/usr/bin/env python3
import math
from collections import deque

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.common.params import Params
from openpilot.common.realtime import DT_DMON, Ratekeeper, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.monitoring.policy import DriverMonitoring


# The service a cabin camera would have provided. Named once and used for every ignore
# list SubMaster takes, so the three can never drift apart - see the note in main().
NO_CABIN_CAMERA_IGNORE = ['driverStateV2']

# Validity we deliberately do not require, on a vehicle with no cabin camera.
#
# extrinsicsCalibration stays `uncalibrated` until the car has been driven, and its valid
# flag is false until then (measured: 2366/2366 messages false over a 10-minute bench run).
# Because all_checks() ANDs validity across every subscribed service, leaving it in made
# "is the device calibrated" decide "does wheel-touch monitoring work at all" - and with no
# cabin camera, wheel-touch IS the whole of driver monitoring. The wheeltouch policy never
# consumes the calibration: policy.py passes cal_rpy to face_orientation_from_model(), but
# every consumer of the resulting pose is gated on self.face_detected, which is false by
# construction here (faceProb 0). So the calibration is genuinely irrelevant to this
# configuration, and requiring it only delays monitoring.
#
# The argument for the other choice, which is real: selfdrived already raises
# calibrationIncomplete with ET.NO_ENTRY and ET.SOFT_DISABLE whenever calStatus is not
# `calibrated` (selfdrived.py:284-287, events.py:822-826), so openpilot cannot engage before
# calibration anyway - meaning the gap could never have produced "engaged with no
# monitoring". On that view this relaxes an upstream precondition for no safety gain.
# The owner chose to have monitoring active from the first drive. Reverting is one line:
# drop this list from the ignore_valid argument below.
#
# Only validity is relaxed. Alive and frequency are NOT: the message does arrive, at 4 Hz,
# and if calibrationd ever stops publishing we still want all_checks() to notice.
CALIBRATION_VALIDITY_NOT_REQUIRED = ['extrinsicsCalibration']


def no_face_driver_state():
  """A driverStateV2 that explicitly reports "the camera sees nothing".

  Not the same as the default message. policy._update_states returns early unless
  faceOrientation / facePosition / their Stds are all populated, and a never-published
  driverStateV2 has them zero-length. That early return happens before the policy switch,
  so the policy would stay in vision mode - where a wheel touch does NOT clear the alert
  (it is gated on the wheeltouch policy), leaving the driver locked out with no way back.
  Populating the lists lets the policy take its designed no-face path to wheeltouch.
  """
  ds = log.DriverStateV2.new_message()
  for side in (ds.leftDriverData, ds.rightDriverData):
    side.faceOrientation = [0., 0., 0.]
    side.facePosition = [0., 0.]
    side.faceOrientationStd = [0., 0., 0.]
    side.facePositionStd = [0., 0.]
    side.faceProb = 0.
  return ds


# Hands-on detection from the driver-torque sensor, for driver monitoring ONLY (2026-11-14).
#
# This car has no cabin camera and the owner decided not to fit one, so wheel touch is the whole
# of driver monitoring. The port's carState.steeringPressed (|DRIVER_TORQUE| > 24 with a
# 5-sample filter, fitted to the vendor's own steeringPressed) is a deliberate-override signal:
# natural holding never reaches it (measured peaks 3-6), so the wheeltouch policy would alert at
# 5/15/25 s with the driver's hands on the wheel. steeringPressed is deliberately NOT lowered to
# fix that: it also drives the lateral-override state (car_events.py), the lane-change nudge
# (desire_helper.py), lagd's sample selection and the port's release of STEER_REQ while the
# driver overrides, and a light touch must not start a lane change. Monitoring alone gets its
# own touch signal here.
#
# The rule ("gated activity", migration-audit/closed-course-20261114/DM-OPTIONS-RESULT.txt):
#   c = steeringTorque - zero + K * inertia * thetaddot
#       the steering wheel's own inertia removed: when the EPS accelerates the column, the
#       torque sensor reads -inertia * thetaddot with no hand on it (fitted 0.050-0.060 kg m^2);
#       the torque median-filtered over TORQUE_MEDIAN_N carStates (single-frame jumps, see feed)
#   while the wheel is quiet (max |thetaddot| over the last 1 s < quiet_accel):
#       touch if the 1 s peak-to-peak of c >= activity      (a resting hand is never still)
#   otherwise:
#       touch if |c| > threshold                              (conservative fallback)
#   held for HANDS_ON_MIN_TICKS consecutive DM ticks.
# Recorded carStates replayed through this class (dm_online_replay.py; its c equals the offline
# chain to 1e-13): over the vendor drive's 997 s of lateral control with hands resting lightly it
# detects 90 % of ticks, longest gap 7.6 s, 3.6 crossings of the 5 s level per hour and none of
# 15 / 25 s, where the plain |tau - zero| > 3.0 detected 45 % with 80 / 17 / 3 crossings per hour.
# At 2026-11-14's low speed, resting lightly, it detects only 31-42 %.
# Hands OFF while the EPS steers, modelled (wheel inertia plus sensor noise on the real thetaddot):
#  - with the "true" thetaddot a smoother estimate than ours, or ours one carState early or late,
#    under 0.1 % of ticks count a touch at the vendor's speeds; without the inertia term, at low
#    speed, up to every 5 s window holds a false touch, which would keep every alert away -
#    but that "true" thetaddot still comes from carState's angle series, so its sampling error is
#    partly in both and cancels;
#  - openpilot-a0's version takes it from the evenly spaced bus frames (Savitzky-Golay over 11 or
#    21 frames) and gives no such assurance. With the torque median, over the vendor's 997 s at
#    50-99 km/h, the share of 15 s windows holding a false touch - one restarts the count, so per
#    window is what matters - is 0 % with the 21-frame truth; with the 11-frame one it is 0 %
#    (true inertia 0.050, torque noise 0.06), 6 % (0.057), 18 % (0.065) and 42 % (0.065, noise
#    0.15). Without the median it reached 98 %, and without the inertia term it is 46 %. It turns
#    on two unmeasured quantities: the torque sensor's noise while driving hands off (0.06
#    standing) and how much fast content the wheel's true acceleration has (0.1 deg angle steps
#    cannot tell). At 2026-11-14's low speed, 22 s, the median changes little and the inertia
#    decides: true 0.065 -> 40 % of 5 s windows, so it is to be refitted from real hands-off data.
# NOT validated, and not to be relied on as hands-off protection at speed until measured: real
# hands-off readings while the EPS steers (sensor noise, column friction, torsion compliance, road
# input). The closed course must show the alerts arriving with the hands off, window by window,
# at the speeds it is to be used at.
HANDS_ON_TORQUE = {
  # zero, threshold, inertia (kg m^2), activity (1 s peak-to-peak), quiet_accel (deg/s^2)
  "BYD_SONG_PLUS_DMI_23": {"zero": -1.1, "threshold": 3.0, "inertia": 0.05, "activity": 1.5, "quiet_accel": 100.0},
}
HANDS_ON_MIN_TICKS = 2
TORQUE_PER_INERTIA_ACCEL = (math.pi / 180) / 0.1   # torque units (0.1 Nm) per kg m^2 * deg/s^2
SAMPLE_DT = 0.01        # carState period: the derivative chain steps on this, not on logMonoTime
WINDOW_N = 100          # 1 s of carState
SMOOTH_N = 6
CHAIN_N = 15            # carStates one centred thetaddot needs
ACCEL_DELAY = 6         # ... and how far that thetaddot lags the newest of them
TORQUE_MEDIAN_N = 5     # carStates the torque is median-filtered over, centred on the same one
GAP_S = 0.1             # a longer pause between carStates restarts the detector


def centred_accel(angles, dt: float = SAMPLE_DT) -> float:
  """thetaddot (deg/s^2) of angles[8], from angles[0..14], 15 consecutive carStates.

  6-point moving average, centred difference, 6-point moving average, centred difference: the
  chain the offline evaluation used (np.convolve mode='same', np.gradient), so the thetaddot of
  carState n-6 is final once carState n has arrived.

  The step is the fixed carState period, NOT the logMonoTime difference. carState is published
  when pandad hands card a batch of CAN, so consecutive logMonoTimes are 2 to 60 ms apart;
  dividing by those differences turned the 0.1 deg angle steps into thetaddot spikes of several
  hundred deg/s^2, and in the hands-off model the compensation term then read as a hand on the
  wheel in half the ticks (replay, 2026-11-14). The fixed step is right for most carStates, not
  all: between two of them 1 new angle frame (0x11F, 100 Hz) arrived in 93.2 %, 0 in 3.4 % and 2
  in 3.4 % (the beat between the bus and pandad's batches; three routes, openpilot-a0). Against
  the same chain run on the evenly spaced bus frames, that costs c an error whose own 1 s
  peak-to-peak is p95 0.73 and at most 1.39 under the vendor's lateral control - half the activity
  level at speed - and p95 1.9 at 2026-11-14's low speed, where the wheel is mostly not quiet.
  """
  assert len(angles) == CHAIN_N
  sm = [sum(angles[i - 3:i + 3]) / SMOOTH_N for i in range(3, 13)]   # sm[k]: centre k + 3
  vel = [(sm[k + 1] - sm[k - 1]) / (2 * dt) for k in range(1, 9)]    # vel[m]: centre m + 4
  vs_7 = sum(vel[0:6]) / SMOOTH_N
  vs_9 = sum(vel[2:8]) / SMOOTH_N
  return (vs_9 - vs_7) / (2 * dt)


class HandsOnTorque:
  """Fed every carState (100 Hz) through feed(); asked once per DM tick through tick().

  The torque is paired with the thetaddot of the same carState, so the detector runs ACCEL_DELAY
  carStates (60 ms) behind; driver monitoring counts seconds, so that costs nothing.
  """
  def __init__(self, zero: float, threshold: float, inertia: float = 0.0, activity: float | None = None,
               quiet_accel: float | None = None, min_ticks: int = HANDS_ON_MIN_TICKS):
    self.zero, self.threshold, self.inertia = zero, threshold, inertia
    self.activity, self.quiet_accel, self.min_ticks = activity, quiet_accel, min_ticks
    self.ticks = 0
    self._t_prev: float | None = None
    self._chain: deque = deque(maxlen=CHAIN_N)       # (steeringTorque, steeringAngleDeg)
    self._window: deque = deque(maxlen=WINDOW_N)     # (c, |thetaddot|), newest last

  @classmethod
  def for_car(cls, car_fingerprint: str) -> 'HandsOnTorque | None':
    values = HANDS_ON_TORQUE.get(car_fingerprint)
    return cls(**values) if values is not None else None

  def feed(self, t: float, steering_torque: float, steering_angle_deg: float) -> None:
    if self._t_prev is not None and not (0. <= t - self._t_prev <= GAP_S):
      # carState paused (or time went backwards): start over rather than difference across it
      self._chain.clear()
      self._window.clear()
    self._t_prev = t
    self._chain.append((steering_torque, steering_angle_deg))
    if len(self._chain) < CHAIN_N:
      return
    chain = list(self._chain)
    accel = centred_accel([s[1] for s in chain])
    # The torque of the same carState, median-filtered over TORQUE_MEDIAN_N carStates around it
    # (all already in, so no further delay). The 0x1FC frames carry single-frame jumps of 1.0 or
    # more: 1894 in the vendor drive, 13 a minute while the EPS steered, half of them away from
    # the zero and 165 of them at hands-off torque levels, almost none at standstill, cause
    # unknown. One 0x1FC frame (50 Hz) spans two carStates, which is why 3 is not enough. Cost:
    # vendor lateral control detected 88.9 % instead of 93.6 %, the same 3.6 five-second crossings
    # per hour (openpilot-a0).
    mid, half = CHAIN_N - 1 - ACCEL_DELAY, TORQUE_MEDIAN_N // 2
    torque = sorted(s[0] for s in chain[mid - half:mid + half + 1])[half]
    c = torque - self.zero + TORQUE_PER_INERTIA_ACCEL * self.inertia * accel
    self._window.append((c, abs(accel)))

  def _hit(self) -> bool:
    if not self._window:
      return False
    c_now = self._window[-1][0]
    if self.activity is None or self.quiet_accel is None:
      return abs(c_now) > self.threshold
    if max(w[1] for w in self._window) < self.quiet_accel:
      cs = [w[0] for w in self._window]
      return max(cs) - min(cs) >= self.activity
    return abs(c_now) > self.threshold

  def tick(self, fresh: bool = True) -> bool:
    """Once per DM tick. fresh=False (no new carState since the last tick) resets, so a stalled
    carState can never be counted as a held touch."""
    if not fresh:
      self.ticks = 0
      return False
    self.ticks = self.ticks + 1 if self._hit() else 0
    return self.ticks >= self.min_ticks


class CarStateHandsOn:
  """carState as driver monitoring sees it: steeringPressed widened by the hands-on detector.
  Every other field passes through untouched, and the real carState is not modified."""
  def __init__(self, car_state, hands_on: bool):
    self._cs = car_state
    self.steeringPressed = car_state.steeringPressed or hands_on

  def __getattr__(self, name):
    return getattr(self._cs, name)


def carstate_feed(sock):
  """Every carState since the last call, as (t, steeringTorque, steeringAngleDeg)."""
  def drain():
    return [(m.logMonoTime * 1e-9, m.carState.steeringTorque, m.carState.steeringAngleDeg)
            for m in messaging.drain_sock(sock)]
  return drain


class SubMasterNoCabinCamera:
  """SubMaster proxy that substitutes a synthetic driverStateV2 for the absent camera and, when
  a hands-on detector is given, widens carState.steeringPressed for driver monitoring."""
  def __init__(self, sm, driver_state, hands_on: HandsOnTorque | None = None, feed=None):
    self._sm = sm
    self._driver_state = driver_state
    self._hands_on = hands_on
    self._feed = feed
    self._hands_on_now = False

  def update(self, timeout=100):
    self._sm.update(timeout)
    # The SubMaster only keeps the latest carState; the detector needs every one (100 Hz) for
    # thetaddot and the 1 s window, so it reads them from its own unconflated socket. It is
    # asked once per update(), i.e. once per DM tick, and only new carStates count
    # (openpilot-1e's review, 2026-11-14).
    if self._hands_on is not None and self._feed is not None:
      samples = self._feed()
      for s in samples:
        self._hands_on.feed(*s)
      self._hands_on_now = self._hands_on.tick(fresh=bool(samples))

  def __getitem__(self, service):
    if service == 'driverStateV2':
      return self._driver_state
    if service == 'carState' and self._hands_on is not None:
      return CarStateHandsOn(self._sm['carState'], self._hands_on_now)
    return self._sm[service]

  def __getattr__(self, name):
    return getattr(self._sm, name)


def dmonitoringd_thread():
  config_realtime_process([0, 1, 2, 3], 5)

  params = Params()
  pm = messaging.PubMaster(['driverMonitoringState'])
  services = ['driverStateV2', 'extrinsicsCalibration', 'carState', 'selfdriveState', 'modelV2']

  # Hardware built without a cabin camera runs no dmonitoringmodeld, so there is no
  # driverStateV2 to pace this loop or to validate. Monitoring is NOT skipped: the policy
  # reads the default driverStateV2, sees faceProb 0, and so selects the wheeltouch policy
  # on its own - the same path upstream takes when the camera cannot see a face. The loop
  # must still run at 1/DT_DMON, because every timeout and lockout in policy.py is counted
  # in DT_DMON ticks; pacing off carState instead would be 100 Hz and would silently turn
  # the 25 s wheeltouch timeout into 5 s.
  cabin_camera_missing = params.get_bool("CabinCameraMissing")
  if cabin_camera_missing:
    # Which car decides whether driver torque counts as a touch (see HANDS_ON_TORQUE). card
    # writes CarParams once it has fingerprinted, a few seconds into the drive.
    CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
    hands_on = HandsOnTorque.for_car(CP.carFingerprint)
    if hands_on is not None:
      cloudlog.warning(f"no cabin camera: torque hands-on detection for {CP.carFingerprint}: "
                       f"{HANDS_ON_TORQUE[CP.carFingerprint]}, {hands_on.min_ticks} ticks")
    else:
      cloudlog.warning(f"no cabin camera: no hands-on torque values for {CP.carFingerprint}; steeringPressed only")
    # All THREE ignore lists have to name driverStateV2, which is why they share one
    # constant instead of being written out three times. all_checks() is
    #     all_alive() and all_freq_ok() and all_valid()        cereal/messaging:277
    # so a service that never arrives fails the *frequency* check even when alive and
    # valid are ignored. Getting only two of the three (the first attempt here) leaves
    # all_checks() permanently False, so dmonitoringd's `elif valid: DM.run_step(...)`
    # never fires and activePolicy sits at its initial `vision` (policy.py:162) forever -
    # while driverMonitoringState still publishes at 20 Hz, so nothing looks wrong from
    # the outside. Measured on the device 2026-10-24.
    sm = SubMasterNoCabinCamera(
      # frequency= is NOT optional here, and leaving it out was the second bug in this
      # branch. SubMaster sizes every service's acceptable rate window against how often
      # the caller calls update():
      #     self.update_freq = frequency or max(SERVICE_LIST[s].frequency
      #                                         for s in polled_services)   messaging:216
      # Upstream passes poll='driverStateV2', so update_freq becomes that service's 20 Hz.
      # Replacing poll= with a Ratekeeper removed that information, and with neither poll=
      # nor frequency= the "conservative" fallback takes the MAX over all subscriptions -
      # 100 Hz, from carState. FrequencyTracker then demands carState at 40..120 Hz
      # (min_freq 50*0.8, max_freq 100*1.2), while a 20 Hz loop can only ever take 20/s
      # because update() does recv_one_or_none per socket per call. freq_ok['carState']
      # stays False, so all_checks() stays False, run_step() never runs, and activePolicy
      # sits at `vision` - with driverMonitoringState still publishing at 20 Hz, so from
      # outside it looks like the policy simply chose vision. Measured on the device
      # 2026-10-24 against replayed real CAN, where every service was alive and valid.
      # With frequency=20: carState 16..24, modelV2 8..24, extrinsicsCalibration 3.2..4.8.
      messaging.SubMaster(services,
                          frequency=1. / DT_DMON,
                          ignore_alive=NO_CABIN_CAMERA_IGNORE,
                          ignore_avg_freq=NO_CABIN_CAMERA_IGNORE,
                          ignore_valid=NO_CABIN_CAMERA_IGNORE + CALIBRATION_VALIDITY_NOT_REQUIRED),
      no_face_driver_state(),
      hands_on,
      carstate_feed(messaging.sub_sock('carState', conflate=False)) if hands_on is not None else None,
    )
    rk: Ratekeeper | None = Ratekeeper(1. / DT_DMON)
  else:
    sm = messaging.SubMaster(services, poll='driverStateV2')
    rk = None

  DM = DriverMonitoring(rhd_saved=params.get_bool("IsRhdDetected"), always_on=params.get_bool("AlwaysOnDM"))
  demo_mode=False
  frame = 0

  # 20Hz <- dmonitoringmodeld, or <- Ratekeeper when there is no cabin camera
  while True:
    if rk is None:
      sm.update()
      if not sm.updated['driverStateV2']:
        # iterate when model has new output
        continue
    else:
      sm.update(0)
      rk.keep_time()
    frame += 1

    valid = sm.all_checks()
    if demo_mode and sm.valid['driverStateV2']:
      DM.run_step(sm, demo=True)
    elif valid:
      DM.run_step(sm, demo=demo_mode)

    # publish
    dat = DM.get_state_packet(valid=valid)
    pm.send('driverMonitoringState', dat)

    # frameId stays 0 without a cabin camera, so pace the periodic work off the loop count
    tick = frame if cabin_camera_missing else sm['driverStateV2'].frameId

    # load live always-on toggle
    if tick % 40 == 1:
      DM.always_on = params.get_bool("AlwaysOnDM")
      demo_mode = params.get_bool("IsDriverViewEnabled")

    # save rhd virtual toggle every 5 mins
    if (tick % 6000 == 0 and not demo_mode and
     DM.wheelpos_offsetter.filtered_stat.n > DM.settings._WHEELPOS_FILTER_MIN_COUNT and
     DM.wheel_on_right == (DM.wheelpos_offsetter.filtered_stat.M > DM.settings._WHEELPOS_THRESHOLD)):
      params.put_bool("IsRhdDetected", DM.wheel_on_right)

def main():
  dmonitoringd_thread()


if __name__ == '__main__':
  main()
