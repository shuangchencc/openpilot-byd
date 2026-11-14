def byd_checksum(address: int, sig, d: bytearray) -> int:
  return (~sum(d[:7])) & 0xFF


def step_rate_limit(current: int, lat_active: bool, engaged: int, idle: int, step: int) -> int:
  # ANGLE_RATE_LIMIT for the next frame: towards `engaged` while asking the EPS to
  # steer and towards `idle` while not, by at most `step` per frame.
  target = engaged if lat_active else idle
  return current + max(-step, min(step, target - current))


def create_steering_control(packer, apply_angle: float, lat_active: bool, counter: int,
                            rate_limit: int = 299, set_me_f: int = 0xF):
  # The rate limit field and SET_ME_F hold what the EPS expects; both differ per
  # platform, so the caller supplies what its car sends (see step_rate_limit).
  # Atto 3: ±299 while steering, 0 while not, SET_ME_F 0xF.
  values = {
    "STEER_REQ": 1 if lat_active else 0,
    "STEER_REQ_ACTIVE_LOW": 0 if lat_active else 1,
    "STEER_ANGLE": apply_angle,
    "ANGLE_RATE_LIMIT_UPPER": rate_limit,
    "ANGLE_RATE_LIMIT_LOWER": -rate_limit,
    "E2E_ALIVE_1": 1,
    "E2E_ALIVE_2": 1,
    "SET_ME_FF": 0xFF,
    "SET_ME_F": set_me_f,
    "COUNTER": counter,
  }
  return packer.make_can_msg("STEERING_MODULE_ADAS", 0, values)


def create_buttons(packer, cancel: bool):
  values = {
    "SET_ME_1_1": 1,
    "SET_ME_1_2": 1,
    "ACC_ON_BTN": 1 if cancel else 0,
  }
  return packer.make_can_msg("PCM_BUTTONS", 0, values)


def create_lkas_hud(packer, lat_active: bool, counter: int, stock_lkas_hud: dict, hud_control, song_plus: bool = False):
  values = {**stock_lkas_hud, "COUNTER": counter, "HANDS_ON_WHEEL_REQ": 0}
  if song_plus:
    # What this car's instrument cluster was sent by the stack that steered it for the
    # whole 116 min recording: the stock camera's frame as it is, and while steering both
    # lane states 2 and LKAS_STATE 3 (every one of 49,810 frames), LKS_MODE left alone.
    # The values below this branch are the Atto 3's; what either set draws on this
    # cluster is not known from data, so nothing else is signalled here, lane departure
    # included: 2 is already the "steering" lane state.
    if lat_active:
      values["LEFT_LANE_STATE"] = 2
      values["RIGHT_LANE_STATE"] = 2
      values["LKAS_STATE"] = 3
    return packer.make_can_msg("LKAS_HUD_ADAS", 0, values)

  if lat_active:
    values["LKS_MODE"] = 2 # green lane line icon
    values["LKAS_STATE"] = 2 # green steering wheel icon
    # LANE_STATE: 0=Grey, 1=Green, 2=Orange
    values["LEFT_LANE_STATE"] = 1 if hud_control.leftLaneVisible else 0
    values["RIGHT_LANE_STATE"] = 1 if hud_control.rightLaneVisible else 0

  if hud_control.leftLaneDepart:
    values["LEFT_LANE_STATE"] = 2
  if hud_control.rightLaneDepart:
    values["RIGHT_LANE_STATE"] = 2

  return packer.make_can_msg("LKAS_HUD_ADAS", 0, values)
