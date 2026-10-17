"""Explicit offline-profile checks at existing cereal consumer boundaries.

No imports of messaging, hardware, Params or the Panda API. Absence of the opt-in
preserves upstream behavior; an invalid supplied profile never falls back to it.
The unchanged cereal schema carries neither identity nor epoch: consumers rely
on the authoritative publisher's Event.valid and the same explicit profile.
"""
from dataclasses import dataclass
import os
from collections.abc import Mapping

from .profile import Profile, ProfileError, load_profile

PROFILE_ENV = "SONGPLUS_PANDA_PROFILE_JSON"
PANDA_MAX_AGE_NS = 250_000_000       # 10 Hz; expiry is exclusive
PERIPHERAL_MAX_AGE_NS = 750_000_000  # 2 Hz; no refreshed source timestamp
CONTROL_MAX_AGE_NS = 100_000_000
CAN_MAX_AGE_NS = 100_000_000
BOARD_TYPES = {"DOS": "dos", "RED_PANDA": "redPanda"}


def fresh(valid: bool, timestamp_ns: int, now_ns: int, max_age_ns: int) -> bool:
  return (valid is True and type(timestamp_ns) is int and type(now_ns) is int
          and 0 < timestamp_ns <= now_ns and now_ns - timestamp_ns < max_age_ns)


def event_fresh(event, name: str, now_ns: int, max_age_ns: int) -> bool:
  try:
    return event.which() == name and fresh(event.valid, event.logMonoTime, now_ns, max_age_ns)
  except (AttributeError, TypeError, ValueError):
    return False


def stream_fresh(sm, name: str, now_ns: int, max_age_ns: int) -> bool:
  """Both original Event time and local arrival age matter, even when no update."""
  try:
    received = int(sm.recv_time[name] * 1_000_000_000)
    return (sm.seen[name] is True and fresh(sm.valid[name], sm.logMonoTime[name], now_ns, max_age_ns)
            and fresh(True, received, now_ns, max_age_ns))
  except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
    return False


@dataclass(frozen=True)
class HardwareObservation:
  valid: bool = False
  ignition: bool = False
  in_car: bool = False
  voltage: int | None = None
  current: int | None = None
  vehicle_type: object = "unknown"


@dataclass(frozen=True)
class MessagePolicy:
  profile: Profile | None
  error: str = ""

  def __post_init__(self):
    if self.profile is not None and type(self.profile) is not Profile:
      raise TypeError("MessagePolicy requires an immutable P0 Profile")

  @property
  def bound(self) -> bool:
    return self.profile is not None and self.profile.binding_status == "SYNTHETIC_BOUND"

  @property
  def fan_required(self) -> bool:
    return self.bound and type(self.profile.roles.fan_control) is int

  def group_ready(self, sm, now_ns: int) -> bool:
    if not self.bound or not stream_fresh(sm, "pandaStates", now_ns, PANDA_MAX_AGE_NS):
      return False
    states = sm['pandaStates']
    return len(states) == 2 and all(str(states[i].pandaType) == BOARD_TYPES[b.expected_board_type]
                                    for i, b in enumerate(self.profile.slots))

  def hardware(self, sm, now_ns: int) -> HardwareObservation:
    if not self.group_ready(sm, now_ns):
      return HardwareObservation()
    states, roles = sm['pandaStates'], self.profile.roles
    power = states[roles.power_monitor] if type(roles.power_monitor) is int else None
    return HardwareObservation(True,
                               any(states[i].ignitionLine or states[i].ignitionCan for i in roles.ignition_sources),
                               str(states[roles.harness].harnessStatus) != "notConnected",
                               None if power is None else power.voltage,
                               None if power is None else power.current,
                               states[roles.vehicle_can].pandaType)

  def fan_rpm(self, sm, now_ns: int) -> int | None:
    if not self.group_ready(sm, now_ns):
      return None
    owner = self.profile.roles.fan_control
    if type(owner) is not int or not stream_fresh(sm, "peripheralState", now_ns, PERIPHERAL_MAX_AGE_NS):
      return None
    state = sm['peripheralState']
    if str(state.pandaType) != BOARD_TYPES[self.profile.slots[owner].expected_board_type]:
      return None
    return state.fanSpeedRpm

  def enabled(self, sm, now_ns: int) -> bool:
    # P0 synthetic fixtures categorically do not authorize actual engagement.
    return (self.bound and self.profile.can_tx_enabled and self.group_ready(sm, now_ns)
            and stream_fresh(sm, 'selfdriveState', now_ns, CONTROL_MAX_AGE_NS)
            and sm['selfdriveState'].enabled)

  def controls_permitted(self, sm, now_ns: int) -> bool:
    return (self.bound and self.profile.can_tx_enabled and self.group_ready(sm, now_ns)
            and stream_fresh(sm, 'carControl', now_ns, CONTROL_MAX_AGE_NS))

  def accept_can(self, event, now_ns: int) -> bool:
    # Reception is observational. Only unflagged or explicitly returned/rejected
    # host buses in the fixed two-slot space are accepted, with payload intact.
    if not self.bound or not event_fresh(event, 'can', now_ns, CAN_MAX_AGE_NS):
      return False
    return all(msg.src in (0, 1, 2, 4, 5, 6, 128, 129, 130, 132, 133, 134,
                           192, 193, 194, 196, 197, 198) for msg in event.can)

  def require_diagnostics_disabled(self) -> None:
    raise RuntimeError("offline dual-Panda profile disables vehicle discovery, diagnostic queries and OBD writes")


def policy_from_environment(environ: Mapping[str, str] | None = None) -> MessagePolicy | None:
  env = os.environ if environ is None else environ
  if PROFILE_ENV not in env:
    return None
  try:
    return MessagePolicy(load_profile(env[PROFILE_ENV]))
  except (ProfileError, TypeError) as error:
    return MessagePolicy(None, str(error))
