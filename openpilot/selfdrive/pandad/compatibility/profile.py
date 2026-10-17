"""Strict, immutable profiles for the offline compatibility implementation."""

from dataclasses import dataclass
import json


SCHEMA = "songplus-dual-panda-offline-profile/v1"
CURRENT_PROTOCOL = "current:f512bcff76f2ab75"
UNSUPPORTED = "UNSUPPORTED"
MAX_PROFILE_BYTES = 32768
# This is a synthetic policy catalog, not evidence of the user's physical PCB.
# Supporting another board family requires an explicit reviewed catalog change.
KNOWN_BOARD_MCU_PAIRS = (("DOS", "F4"), ("RED_PANDA", "H7"))


class ProfileError(ValueError):
  pass


def _require(condition: bool, message: str) -> None:
  if not condition:
    raise ProfileError(message)


def _text(value: object) -> bool:
  return type(value) is str and 0 < len(value) <= 128 and value == value.strip() and all(32 < ord(c) < 127 for c in value)


def _slot(value: object) -> bool:
  return type(value) is int and value in (0, 1)


@dataclass(frozen=True)
class Capabilities:
  can_buses: tuple[int, ...]
  can_fd: bool
  diagnostic: bool
  harness: bool
  ignition: bool
  fan: bool
  power_monitor: bool
  ir: bool

  def __post_init__(self) -> None:
    _require(type(self.can_buses) is tuple and all(type(x) is int for x in self.can_buses)
             and self.can_buses == (0, 1, 2), "CAN buses must be exactly (0, 1, 2)")
    for key in ("can_fd", "diagnostic", "harness", "ignition", "fan", "power_monitor", "ir"):
      _require(type(getattr(self, key)) is bool, f"capability {key} must be boolean")


@dataclass(frozen=True)
class SlotBinding:
  slot: int
  identity_binding: str | None
  expected_board_type: str | None
  mcu_family: str | None
  exact_mcu: str | None
  transport: str | None
  protocol_profile: str | None
  capabilities: Capabilities | None
  application_signature: str | None
  safety_config_semantics: str | None

  def __post_init__(self) -> None:
    _require(_slot(self.slot), "slot must be integer 0 or 1")
    values = [getattr(self, name) for name in self.__dataclass_fields__ if name != "slot"]
    if all(value is None for value in values):
      return
    _require(all(value is not None for value in values), "binding must be fully specified or completely unbound")
    for name in self.__dataclass_fields__:
      if name not in ("slot", "capabilities"):
        _require(_text(getattr(self, name)), f"binding {name} must be nonempty bounded ASCII")
    _require(self.identity_binding.startswith("fixture:"), "only fixture: identities are permitted")
    _require(self.exact_mcu.startswith("fixture:"), "MCU declaration must be explicitly synthetic")
    _require(self.application_signature.startswith("fixture:"), "application signature must be explicitly synthetic")
    _require(self.transport in ("USB", "SPI"), "unsupported transport")
    _require(self.mcu_family in ("F4", "H7"), "unsupported MCU family")
    _require((self.expected_board_type, self.mcu_family) in KNOWN_BOARD_MCU_PAIRS,
             "unknown board type or incompatible board/MCU family")
    _require(self.protocol_profile == CURRENT_PROTOCOL, "only the locked current control ABI can bind")
    _require(type(self.capabilities) is Capabilities, "capabilities must be explicit")
    _require(self.mcu_family != "F4" or not self.capabilities.can_fd, "F4 cannot declare CAN-FD")
    _require(self.safety_config_semantics == "noOutput", "P0 fixture safety must be noOutput")


@dataclass(frozen=True)
class Roles:
  vehicle_can: int | str | None
  diagnostic: int | str | None
  harness: int | str | None
  ignition_sources: tuple[int, ...] | None
  fan_control: int | str | None
  power_monitor: int | str | None
  ir_control: int | str | None

  def __post_init__(self) -> None:
    for name in self.__dataclass_fields__:
      value = getattr(self, name)
      if name == "ignition_sources":
        _require(value is None or (type(value) is tuple and len(value) in (1, 2)
                 and all(_slot(x) for x in value) and len(set(value)) == len(value)), "invalid ignition_sources")
      else:
        _require(value is None or _slot(value) or (type(value) is str and value == UNSUPPORTED), f"invalid role {name}")


@dataclass(frozen=True)
class Profile:
  schema: str
  purpose: str
  binding_status: str
  device_access_enabled: bool
  can_tx_enabled: bool
  firmware_install_enabled: bool
  expected_board_count: int
  required_slots: tuple[int, ...]
  bus_stride: int
  ordinary_local_can_buses: tuple[int, ...]
  slots: tuple[SlotBinding, ...]
  roles: Roles

  def __post_init__(self) -> None:
    _require(self.schema == SCHEMA, "unknown profile schema")
    _require(self.purpose == "offline_fixture", "profile is for offline fixtures only")
    _require(type(self.binding_status) is str and self.binding_status in ("UNBOUND", "SYNTHETIC_BOUND"), "invalid binding status")
    for name in ("device_access_enabled", "can_tx_enabled", "firmware_install_enabled"):
      _require(getattr(self, name) is False, f"{name} must remain false")
    _require(type(self.expected_board_count) is int and self.expected_board_count == 2, "exactly two required boards")
    _require(type(self.required_slots) is tuple and all(type(x) is int for x in self.required_slots)
             and self.required_slots == (0, 1), "required slots must be exactly (0, 1)")
    _require(type(self.bus_stride) is int and self.bus_stride == 4, "bus stride must be 4")
    _require(type(self.ordinary_local_can_buses) is tuple and all(type(x) is int for x in self.ordinary_local_can_buses)
             and self.ordinary_local_can_buses == (0, 1, 2), "ordinary local CAN buses must be (0, 1, 2)")
    _require(type(self.slots) is tuple and len(self.slots) == 2 and all(type(s) is SlotBinding for s in self.slots), "invalid slots")
    _require(tuple(s.slot for s in self.slots) == (0, 1), "profile slots must be canonical, unique and fixed")
    _require(type(self.roles) is Roles, "invalid roles")
    if self.binding_status == "UNBOUND":
      _require(all(s.identity_binding is None for s in self.slots), "unbound profile cannot contain bindings")
      _require(all(getattr(self.roles, n) is None for n in self.roles.__dataclass_fields__), "unbound profile cannot assign roles")
      return
    _require(all(s.identity_binding is not None for s in self.slots), "synthetic profile needs both bindings")
    _require(len({s.identity_binding for s in self.slots}) == 2, "duplicate bound identity")
    role_caps = {"vehicle_can": None, "diagnostic": "diagnostic", "harness": "harness", "fan_control": "fan",
                 "power_monitor": "power_monitor", "ir_control": "ir"}
    for role, cap in role_caps.items():
      owner = getattr(self.roles, role)
      if owner == UNSUPPORTED and role in ("fan_control", "power_monitor", "ir_control"):
        _require(not any(getattr(s.capabilities, cap) for s in self.slots), f"{role} cannot hide a declared capability")
      else:
        _require(_slot(owner), f"{role} needs an explicit slot")
        _require(cap is None or getattr(self.slots[owner].capabilities, cap), f"{role} owner lacks capability")
    _require(self.roles.ignition_sources is not None, "ignition sources must be explicit")
    _require(all(self.slots[s].capabilities.ignition for s in self.roles.ignition_sources), "ignition owner lacks capability")


def _object(pairs: list[tuple[str, object]]) -> dict:
  result = {}
  for key, value in pairs:
    if key in result:
      raise ProfileError(f"duplicate JSON key: {key}")
    result[key] = value
  return result


def _fields(value: object, cls: type) -> dict:
  _require(type(value) is dict, f"{cls.__name__} must be an object")
  _require(set(value) == set(cls.__dataclass_fields__), f"{cls.__name__} fields must match schema exactly")
  return dict(value)


def _tuple(value: object, name: str) -> tuple:
  _require(type(value) is list, f"{name} must be a JSON array")
  return tuple(value)


def load_profile(serialized: str | bytes) -> Profile:
  """Parse supplied JSON without opening a file or importing any hardware API."""
  _require(type(serialized) in (str, bytes), "profile input must be str or bytes")
  try:
    raw = serialized.encode("utf-8") if type(serialized) is str else serialized
    _require(len(raw) <= MAX_PROFILE_BYTES, "profile exceeds size limit")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ProfileError("non-finite JSON value")))
  except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
    raise ProfileError("invalid JSON profile") from error
  values = _fields(data, Profile)
  for field in ("required_slots", "ordinary_local_can_buses", "slots"):
    values[field] = _tuple(values[field], field)
  slots = []
  for value in values["slots"]:
    fields = _fields(value, SlotBinding)
    if fields["capabilities"] is not None:
      caps = _fields(fields["capabilities"], Capabilities)
      caps["can_buses"] = _tuple(caps["can_buses"], "can_buses")
      fields["capabilities"] = Capabilities(**caps)
    slots.append(SlotBinding(**fields))
  values["slots"] = tuple(slots)
  roles = _fields(values["roles"], Roles)
  if roles["ignition_sources"] is not None:
    roles["ignition_sources"] = _tuple(roles["ignition_sources"], "ignition_sources")
  values["roles"] = Roles(**roles)
  return Profile(**values)
