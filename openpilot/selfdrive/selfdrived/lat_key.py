"""Song PLUS lateral key: the steering wheel's custom key engages lateral control alone.

opendbc's BYD port publishes the custom key's short press as the lkas button (BydFlags.CUSTOM_KEY)
and panda keeps its own rule for the same press (byd.h, the lateral key; BydSafetyFlags.LAT_KEY).
card sets that panda bit only for a launch that asks for it (songplus_lat_key below), so the bit
in CarParams is what turns this on here as well: openpilot and panda cannot disagree about it.

The rule, the same on both sides (the owner's, 2026-11-15: the key is an on/off switch for
steering, and the pedals are the driver's alone):
- a press with openpilot not engaged engages it, whatever the stock ACC and the pedals are doing
  ("lateral only"). The usual no-entry conditions still apply (gear, seatbelt, doors, ...),
  except the ACC being unavailable and a pedal being pressed;
- while engaged by the key, neither the ACC being off, nor it engaging or leaving, nor the brake
  ends it (no pcmDisable, wrongCarMode, pedalPressed; panda: brake_keeps_controls); everything
  else does, as before;
- a firm turn of the wheel ends a key engagement (the owner: the driver taking over), see
  OVERRIDE_* below; a lighter one only makes the port withdraw its request for a moment, as in
  any engagement;
- a press while openpilot is engaged ends it, however it was engaged. The stock ACC is left as it
  is: if it was on, it goes on controlling the speed by itself.
Longitudinal stays the stock ACC's: controlsd never sets longActive on a pcmCruise car while the
ACC is not engaged, card does not turn the key on in a launch with openpilot longitudinal, and
panda never lets ACC_CMD be replaced while the key engaged it.
"""
from openpilot.cereal import log
from opendbc.car.structs import car

EventName = log.OnroadEvent.EventName
ButtonType = car.CarState.ButtonEvent.Type

# opendbc/car/byd/values.py BydSafetyFlags, which this file does not import so that it does not
# depend on the BYD port being present
BYD_SAFETY_SONG_PLUS = 1
BYD_SAFETY_LAT_KEY = 16   # bit 4 (bit 3 is the longitudinal work's)


# A key engagement ends when the driver's torque (CarState.steeringTorque, STEERING_TORQUE.DRIVER_TORQUE,
# the DBC's scale, about 0.1 Nm) is above OVERRIDE_TORQUE for OVERRIDE_FRAMES carStates in a row; with a
# blinker on (a lane change, whose push the port lets go up to 35) above OVERRIDE_TORQUE_BLINKER.
# From the routes: engaged, ordinary corrections peaked at 25.9-27.5 (00000033), lane change pushes at
# 26.2-32.5 (0000002e), the owner's deliberate turns against the wheel at 33.0-37.4 (0000002a); not
# engaged and moving the torque never passed 26.1. The EPS lets go within 30-40 ms of a push above 24,
# so the torque of a firm turn is a short peak: the frames are few on purpose.
OVERRIDE_TORQUE = 32.0
OVERRIDE_TORQUE_BLINKER = 40.0
OVERRIDE_FRAMES = 3   # 30 ms at 100 Hz


def songplus_lat_key(environ) -> bool:
  """card: may this launch use the lateral key? Only with control allowed at all, and only when the
  launcher asks for it (SONGPLUS_LAT_KEY=1). Nothing stored on the device turns it on."""
  return environ.get("SONGPLUS_ALLOW_CONTROL") == "1" and environ.get("SONGPLUS_LAT_KEY") == "1"


def lat_key_enabled(CP) -> bool:
  return CP.brand == "byd" and not CP.passive and len(CP.safetyConfigs) > 0 and \
         CP.safetyConfigs[0].safetyModel == car.CarParams.SafetyModel.byd and \
         (CP.safetyConfigs[0].safetyParam & (BYD_SAFETY_SONG_PLUS | BYD_SAFETY_LAT_KEY)) == (BYD_SAFETY_SONG_PLUS | BYD_SAFETY_LAT_KEY)


class LatKey:
  def __init__(self, CP):
    self.on = lat_key_enabled(CP)
    self.lat_only = False   # engaged by the key, with the ACC off
    self.pending = False    # asked to engage in this step
    self.override_frames = 0

  def update(self, events, CS, enabled: bool) -> None:
    """After the car's events and the brake rule, before the state machine. `enabled` is openpilot's
    state from the last step."""
    self.pending = False
    if not self.on:
      return
    if not enabled:
      self.lat_only = False

    limit = OVERRIDE_TORQUE_BLINKER if (CS.leftBlinker or CS.rightBlinker) else OVERRIDE_TORQUE
    self.override_frames = self.override_frames + 1 if abs(CS.steeringTorque) > limit else 0
    if self.lat_only and enabled and self.override_frames >= OVERRIDE_FRAMES:
      events.add(EventName.steerDisengage)
      return

    if any(be.type == ButtonType.lkas and be.pressed for be in CS.buttonEvents):
      if enabled:
        events.add(EventName.buttonCancel)
        return
      events.add(EventName.buttonEnable)
      self.pending = True

    if self.lat_only or self.pending:
      for name in (EventName.pcmDisable, EventName.wrongCarMode, EventName.pedalPressed):
        events.remove(name)

  def after_state_machine(self, enabled: bool) -> None:
    if self.pending and enabled:
      self.lat_only = True
    self.pending = False
