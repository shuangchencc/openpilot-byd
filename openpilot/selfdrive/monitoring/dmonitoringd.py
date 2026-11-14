#!/usr/bin/env python3
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
# (desire_helper.py) and lagd's sample selection, and a light touch must not start a lane
# change. Instead monitoring alone counts |steeringTorque - zero| > threshold, held for
# HANDS_ON_MIN_TICKS consecutive DM ticks (100 ms, against single bumps), as a touch.
#
# Values from openpilot-a0's labelled capture of 2026-11-13 (migration-audit/
# steer-torque-threshold-20261112/RESULT.txt), in carState.steeringTorque units: untouched
# -1.33 +/- 0.06 at standstill, natural two-hand hold mean +2.64, one hand mean +3.41, driving
# zero -1.1. With zero -1.1 / threshold 3.0, untouched never triggers; two hands trigger in 60 %
# of samples and one hand in 90 %.
# NOT yet validated: the reading with the hands OFF while the EPS is steering. If the EPS's own
# action reads above the threshold, monitoring would count a touch that never happened, so this
# must be measured at the closed course before it is relied on.
HANDS_ON_TORQUE = {
  "BYD_SONG_PLUS_DMI_23": (-1.1, 3.0),   # (zero, threshold)
}
HANDS_ON_MIN_TICKS = 2


class HandsOnTorque:
  def __init__(self, zero: float, threshold: float, min_ticks: int = HANDS_ON_MIN_TICKS):
    self.zero, self.threshold, self.min_ticks = zero, threshold, min_ticks
    self.ticks = 0

  @classmethod
  def for_car(cls, car_fingerprint: str) -> 'HandsOnTorque | None':
    values = HANDS_ON_TORQUE.get(car_fingerprint)
    return cls(*values) if values is not None else None

  def update(self, steering_torque: float) -> bool:
    """Call once per DM tick."""
    self.ticks = self.ticks + 1 if abs(steering_torque - self.zero) > self.threshold else 0
    return self.ticks >= self.min_ticks


class CarStateHandsOn:
  """carState as driver monitoring sees it: steeringPressed widened by the hands-on detector.
  Every other field passes through untouched, and the real carState is not modified."""
  def __init__(self, car_state, hands_on: bool):
    self._cs = car_state
    self.steeringPressed = car_state.steeringPressed or hands_on

  def __getattr__(self, name):
    return getattr(self._cs, name)


class SubMasterNoCabinCamera:
  """SubMaster proxy that substitutes a synthetic driverStateV2 for the absent camera and, when
  a hands-on detector is given, widens carState.steeringPressed for driver monitoring."""
  def __init__(self, sm, driver_state, hands_on: HandsOnTorque | None = None):
    self._sm = sm
    self._driver_state = driver_state
    self._hands_on = hands_on
    self._hands_on_now = False

  def update(self, timeout=100):
    self._sm.update(timeout)
    # Once per update() - i.e. once per DM tick - not per access: run_step reads carState
    # several times per tick and the detector counts ticks. Only a NEW carState advances it; a
    # stale one resets it, so a stalled carState can never be counted as a held touch
    # (openpilot-1e's review, 2026-11-14).
    if self._hands_on is not None:
      if self._sm.updated['carState']:
        self._hands_on_now = self._hands_on.update(self._sm['carState'].steeringTorque)
      else:
        self._hands_on.ticks = 0
        self._hands_on_now = False

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
      cloudlog.warning(f"no cabin camera: torque hands-on detection for {CP.carFingerprint}, "
                       f"|steeringTorque - ({hands_on.zero})| > {hands_on.threshold} for {hands_on.min_ticks} ticks")
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
