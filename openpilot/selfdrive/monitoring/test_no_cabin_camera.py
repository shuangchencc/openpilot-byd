"""Driver monitoring on hardware built without a cabin camera.

Some devices ship with no cabin (driver) camera at all, so dmonitoringmodeld cannot run and
driverStateV2 is never published. dmonitoringd keeps running: with no face to see, policy.py
selects its wheeltouch policy, which is the same path upstream takes on a camera-equipped car
whose camera cannot make out a face. test_monitoring.py already covers that path.

What is specific here - and the reason dmonitoringd synthesizes a driverStateV2 rather than
letting the default one through - is the trap in the first test below.
"""
from openpilot.common.test import OpenpilotTestCase
from openpilot.cereal import log
from openpilot.common.realtime import DT_DMON
from openpilot.selfdrive.monitoring.dmonitoringd import no_face_driver_state
from openpilot.selfdrive.monitoring.policy import DriverMonitoring

MonitoringPolicy = log.DriverMonitoringState.MonitoringPolicy

TEST_TIMESPAN = 120  # seconds
N = int(TEST_TIMESPAN / DT_DMON)


def _run(msg, interaction):
  DM = DriverMonitoring()
  alert_lvls = []
  for idx in range(N):
    DM._update_states(msg, [0, 0, 0], 0, True, False)
    DM._update_events(interaction[idx], True, False, 0)
    alert_lvls.append(DM.alert_level)
  return alert_lvls, DM


NEVER_TOUCHES = [False] * N
# a touch every 5 s, well inside the 25 s wheeltouch timeout
TOUCHES_EVERY_5S = [(i % int(5 / DT_DMON)) == 0 for i in range(N)]


class TestNoCabinCamera(OpenpilotTestCase):
  def test_default_message_would_strand_the_driver(self):
    # Why dmonitoringd must not just pass the default driverStateV2 through. Its
    # faceOrientation / facePosition lists are zero-length, so _update_states returns early
    # (policy.py "if not all(len(x) > 0 ...)") - before the line that picks the policy. The
    # policy therefore stays on its initial value, vision, and the wheel-touch reset in
    # _update_events is gated on the wheeltouch policy: the driver would escalate to a red
    # alert and a lockout that touching the wheel cannot clear.
    default = log.DriverStateV2.new_message()
    _, DM = _run(default, TOUCHES_EVERY_5S)
    assert DM.active_policy == MonitoringPolicy.vision, "if this ever becomes wheeltouch, the synthesis is no longer needed"
    assert DM.lockout_active, "the trap: locked out despite touching the wheel every 5 s"

  def test_synthesized_state_selects_wheeltouch(self):
    _, DM = _run(no_face_driver_state(), NEVER_TOUCHES)
    assert not DM.face_detected
    assert DM.active_policy == MonitoringPolicy.wheeltouch

  def test_escalates_and_locks_out_when_the_wheel_is_never_touched(self):
    alert_lvls, DM = _run(no_face_driver_state(), NEVER_TOUCHES)
    s = DM.settings
    assert alert_lvls[int(s._WHEELTOUCH_POLICY_ALERT_1_TIMEOUT / 2 / DT_DMON)] == 0
    assert alert_lvls[int((s._WHEELTOUCH_POLICY_ALERT_1_TIMEOUT +
                           (s._WHEELTOUCH_POLICY_ALERT_2_TIMEOUT - s._WHEELTOUCH_POLICY_ALERT_1_TIMEOUT) / 2) / DT_DMON)] == 1
    assert alert_lvls[int((s._WHEELTOUCH_POLICY_ALERT_2_TIMEOUT +
                           (s._WHEELTOUCH_POLICY_ALERT_3_TIMEOUT - s._WHEELTOUCH_POLICY_ALERT_2_TIMEOUT) / 2) / DT_DMON)] == 2
    assert alert_lvls[-1] == 3
    assert DM.lockout_active, "an untouched wheel must still reach lockout with no camera"

  def test_wheel_touches_keep_it_clear(self):
    # carState.steeringPressed is what feeds this on a real car; on the Song PLUS it comes
    # from DRIVER_TORQUE > 24 Nm, which reproduces the logged steeringPressed at 100.000%.
    alert_lvls, DM = _run(no_face_driver_state(), TOUCHES_EVERY_5S)
    assert DM.active_policy == MonitoringPolicy.wheeltouch
    assert not DM.lockout_active
    assert max(alert_lvls) == 0, f"regular wheel touches should never alert, peaked at {max(alert_lvls)}"
