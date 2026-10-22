#!/usr/bin/env python3
import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from openpilot.common.params import Params
from openpilot.common.realtime import DT_DMON, Ratekeeper, config_realtime_process
from openpilot.selfdrive.monitoring.policy import DriverMonitoring


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


class SubMasterNoCabinCamera:
  """SubMaster proxy that substitutes a synthetic driverStateV2 for the absent camera."""
  def __init__(self, sm, driver_state):
    self._sm = sm
    self._driver_state = driver_state

  def __getitem__(self, service):
    return self._driver_state if service == 'driverStateV2' else self._sm[service]

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
    sm = SubMasterNoCabinCamera(
      messaging.SubMaster(services, ignore_alive=['driverStateV2'], ignore_valid=['driverStateV2']),
      no_face_driver_state(),
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
