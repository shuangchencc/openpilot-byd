"""Song PLUS lateral key: the steering wheel's custom key engages lateral control alone.

opendbc's BYD port publishes the custom key's short press as the lkas button (BydFlags.CUSTOM_KEY)
and panda keeps its own rule for the same press (byd.h, the lateral key; BydSafetyFlags.LAT_KEY).
card sets that panda bit only for a launch that asks for it (songplus_lat_key below), so the bit
in CarParams is what turns this on here as well: openpilot and panda cannot disagree about it.

The rule, the same on both sides:
- a press with openpilot not engaged, the stock ACC not engaged and the brake released engages
  openpilot with the stock ACC off ("lateral only"). The usual no-entry conditions still apply
  (gear, seatbelt, doors, ...), except that the ACC being unavailable is not one;
- while lateral only, the ACC being off does not end it (no pcmDisable, no wrongCarMode);
  everything else does, as before: the brake, faults, ... ;
- a press while lateral only ends it; a press while the ACC is engaged does nothing;
- the ACC engaging while lateral only makes it an ordinary engagement, which ends with the ACC.
Longitudinal stays the stock ACC's: controlsd never sets longActive on a pcmCruise car while the
ACC is not engaged.
"""
from openpilot.cereal import log
from opendbc.car.structs import car

EventName = log.OnroadEvent.EventName
ButtonType = car.CarState.ButtonEvent.Type

# opendbc/car/byd/values.py BydSafetyFlags, which this file does not import so that it does not
# depend on the BYD port being present
BYD_SAFETY_SONG_PLUS = 1
BYD_SAFETY_LAT_KEY = 8


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

  def update(self, events, CS, enabled: bool) -> None:
    """After the car's events, before the state machine. `enabled` is openpilot's state from the last step."""
    self.pending = False
    if not self.on:
      return
    if CS.cruiseState.enabled or not enabled:
      self.lat_only = False

    if any(be.type == ButtonType.lkas and be.pressed for be in CS.buttonEvents) and not CS.cruiseState.enabled:
      if enabled:
        if self.lat_only:
          events.add(EventName.buttonCancel)
      elif not CS.brakePressed:
        # panda refuses the press with the brake held, so must we
        events.add(EventName.buttonEnable)
        self.pending = True

    if self.lat_only or self.pending:
      events.remove(EventName.pcmDisable)
      events.remove(EventName.wrongCarMode)

  def after_state_machine(self, enabled: bool) -> None:
    if self.pending and enabled:
      self.lat_only = True
    self.pending = False
