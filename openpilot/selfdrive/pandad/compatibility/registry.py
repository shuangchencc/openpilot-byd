"""Atomic registration of supplied synthetic observations; no discovery IO."""

from dataclasses import dataclass

from .profile import Capabilities, Profile, SlotBinding


class RegistryError(ValueError):
  def __init__(self, code: str):
    self.code = code
    super().__init__(code)


@dataclass(frozen=True)
class Observation:
  identity: str
  board_type: str
  mcu_family: str
  exact_mcu: str
  transport: str
  protocol_profile: str
  capabilities: Capabilities
  application_signature: str

  def __post_init__(self) -> None:
    # Reuse exactly the bound-profile contract, with the observation's noOutput
    # fixture semantics. No board type is inferred from transport or ordering.
    SlotBinding(0, self.identity, self.board_type, self.mcu_family, self.exact_mcu,
                self.transport, self.protocol_profile, self.capabilities,
                self.application_signature, "noOutput")


@dataclass(frozen=True)
class Registry:
  """Snapshot for simulation, not a live connection or permission token.

  A caller must register the complete observation set on each reconciliation.
  Disconnect/reconnect epochs and real control-session lifecycle belong to P3.
  """
  profile: Profile
  boards: tuple[Observation, ...]

  def __post_init__(self) -> None:
    _validate(self.profile, self.boards)
    if tuple(o.identity for o in self.boards) != tuple(s.identity_binding for s in self.profile.slots):
      raise RegistryError("NONCANONICAL_REGISTRY")

  @property
  def device_access_enabled(self) -> bool:
    return False

  @property
  def can_tx_enabled(self) -> bool:
    return False

  @property
  def firmware_install_enabled(self) -> bool:
    return False

  def for_role(self, role: str) -> tuple[Observation, ...]:
    if type(role) is not str or role not in self.profile.roles.__dataclass_fields__:
      raise RegistryError("UNKNOWN_ROLE")
    slots = getattr(self.profile.roles, role)
    if slots == "UNSUPPORTED":
      return ()
    return tuple(self.boards[s] for s in (slots if type(slots) is tuple else (slots,)))


def _validate(profile: Profile, observations: tuple[Observation, ...]) -> None:
  if type(profile) is not Profile:
    raise RegistryError("INVALID_PROFILE")
  if profile.binding_status != "SYNTHETIC_BOUND":
    raise RegistryError("UNBOUND_PROFILE")
  if type(observations) is not tuple or not all(type(o) is Observation for o in observations):
    raise RegistryError("INVALID_OBSERVATIONS")
  if len(observations) != 2:
    raise RegistryError("REQUIRED_BOARD_COUNT")
  identities = [o.identity for o in observations]
  if len(set(identities)) != 2:
    raise RegistryError("DUPLICATE_IDENTITY")
  by_identity = {o.identity: o for o in observations}
  if set(by_identity) != {slot.identity_binding for slot in profile.slots}:
    raise RegistryError("UNEXPECTED_IDENTITY")
  fields = {"board_type": "expected_board_type", "mcu_family": "mcu_family", "exact_mcu": "exact_mcu", "transport": "transport",
            "protocol_profile": "protocol_profile", "capabilities": "capabilities", "application_signature": "application_signature"}
  for binding in profile.slots:
    observation = by_identity[binding.identity_binding]
    for observed, expected in fields.items():
      if getattr(observation, observed) != getattr(binding, expected):
        raise RegistryError(f"MISMATCH_{observed.upper()}")


def register(profile: Profile, observations: tuple[Observation, ...]) -> Registry:
  """Validate the entire inventory and bind it to fixed profile slots atomically.

  Reversed discovery order is accepted. Missing, extra, duplicate (including
  the same identity on USB and SPI), or substituted boards reject the group.
  """
  _validate(profile, observations)
  by_identity = {observation.identity: observation for observation in observations}
  return Registry(profile, tuple(by_identity[slot.identity_binding] for slot in profile.slots))
