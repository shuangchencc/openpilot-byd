"""Shared look of the Song PLUS AR onroad view: palette, CJK HUD font, small drawing helpers.

Design record: migration-audit/ui-fsd-mockups-20261115 (v3-combo, chosen by the owner 2026-11-15).
"""
import math
from enum import Enum

import pyray as rl

from openpilot.cereal import log
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.system.ui.lib.application import FONT_DIR, FONT_SCALE, gui_app, FontWeight

AlertStatus = log.SelfdriveState.AlertStatus
AlertSize = log.SelfdriveState.AlertSize

TEAL = rl.Color(0x19, 0xE3, 0xC4, 255)
TEAL_LIGHT = rl.Color(0xBF, 0xF7, 0xEC, 255)
AMBER = rl.Color(0xFF, 0xB0, 0x20, 255)
RED = rl.Color(0xFF, 0x3B, 0x30, 255)
RED_BANNER = rl.Color(0xD9, 0x2B, 0x22, 240)
AMBER_BANNER = rl.Color(0xD9, 0x7A, 0x14, 240)
GREY = rl.Color(0xA7, 0xAD, 0xB5, 255)
PATH_GREY = rl.Color(0xD0, 0xD4, 0xDA, 255)
WHITE = rl.Color(255, 255, 255, 255)
GLASS = rl.Color(0x0B, 0x0D, 0x10, 170)
GLASS_EDGE = rl.Color(255, 255, 255, 38)

# Every CJK character the HUD can draw; the subset font holds exactly these (plus ASCII).
HUD_FONT_FILE = "NotoSansSC-Bold-hud.otf"
HUD_CJK = "辅助驾驶中未开启请接管设定方向盘加速度前车车速距时间盲区有已暂停驾驶员覆盖"
HUD_EXTRA = "°²·–−"
INTER_SYMBOLS = "°–"  # non-ASCII characters the Inter fonts are loaded with


class Mode(Enum):
  OFF = "off"            # not engaged: grey centre line, grey wheel
  ENGAGED = "engaged"    # teal carpet
  OVERRIDE = "override"  # driver override: carpet kept, wheel grey
  ALERT = "alert"        # critical alert: take over now, everything red, carpet removed


def current_mode() -> Mode:
  sm = ui_state.sm
  ss = sm['selfdriveState']
  if sm.recv_frame['selfdriveState'] >= ui_state.started_frame and ss.alertSize != AlertSize.none and ss.alertStatus == AlertStatus.critical:
    return Mode.ALERT
  if ui_state.status == UIStatus.ENGAGED:
    return Mode.ENGAGED
  if ui_state.status == UIStatus.OVERRIDE:
    return Mode.OVERRIDE
  return Mode.OFF


def alpha(c: rl.Color, a: float) -> rl.Color:
  return rl.Color(c.r, c.g, c.b, int(max(0, min(255, a))))


class HudFont:
  _font = None  # rl.Font, loaded on first use (rl.Font is a function, so no "| None" annotation)

  @classmethod
  def get(cls) -> rl.Font:
    if cls._font is None:
      chars = {chr(c) for c in range(32, 127)} | set(HUD_CJK) | set(HUD_EXTRA)
      cps = sorted(map(ord, chars))
      buf = rl.ffi.new("int[]", cps)
      font = rl.load_font_ex(str(FONT_DIR / HUD_FONT_FILE), 96, rl.ffi.cast("int *", buf), len(cps))
      if font.glyphCount == 0:  # file missing: fall back to the bold Latin font
        font = gui_app.font(FontWeight.BOLD)
      else:
        rl.gen_texture_mipmaps(font.texture)
        rl.set_texture_filter(font.texture, rl.TextureFilter.TEXTURE_FILTER_TRILINEAR)
      cls._font = font
    return cls._font


def _font_for(s: str, latin: rl.Font) -> rl.Font:
  # Chinese labels (and the few symbols Inter lacks, e.g. "²") use the HUD font; digits and units keep the bold
  # Latin (Inter) fonts in every UI language
  return HudFont.get() if any(ord(ch) > 127 and ch not in INTER_SYMBOLS for ch in s) else latin


def _measure(font: rl.Font, s: str, size: float) -> float:
  # not measure_text_cached: it swaps in the UI language's fallback font, and the HUD never draws with that
  return rl.measure_text_ex(font, s, size * FONT_SCALE, 0).x  # noqa: TID251


def text_width(s: str, size: float, latin: rl.Font) -> float:
  return _measure(_font_for(s, latin), s, size)


def draw_text(s: str, x: float, y: float, size: float, color: rl.Color, latin: rl.Font, anchor: str = "l", shadow: bool = True) -> float:
  font = _font_for(s, latin)
  w = _measure(font, s, size)
  if anchor == "c":
    x -= w / 2
  elif anchor == "r":
    x -= w
  # gui_app wraps rl.draw_text_ex to swap in the UI language's fallback font (regular weight, and lacking the HUD's
  # characters); draw with the unwrapped function and apply the same global scale
  draw = getattr(rl, "_orig_draw_text_ex", rl.draw_text_ex)
  size = size * FONT_SCALE
  if shadow:
    draw(font, s, rl.Vector2(x + 1.5, y + 2), size, 0, rl.Color(0, 0, 0, int(110 * color.a / 255)))
  draw(font, s, rl.Vector2(x, y), size, 0, color)
  return w


def rounded(x: float, y: float, w: float, h: float, r: float, color: rl.Color) -> None:
  rl.draw_rectangle_rounded(rl.Rectangle(x, y, w, h), min(1.0, 2 * r / min(w, h)), 16, color)


def rounded_lines(x: float, y: float, w: float, h: float, r: float, th: float, color: rl.Color) -> None:
  rl.draw_rectangle_rounded_lines_ex(rl.Rectangle(x, y, w, h), min(1.0, 2 * r / min(w, h)), 16, th, color)


def glass(x: float, y: float, w: float, h: float, r: float, a: int = 170) -> None:
  rounded(x, y, w, h, r, alpha(GLASS, a))
  rounded_lines(x, y, w, h, r, 1.5, GLASS_EDGE)


def wheel_icon(cx: float, cy: float, r: float, color: rl.Color) -> None:
  th = max(4.0, r * 0.2)
  rl.draw_ring(rl.Vector2(cx, cy), r - th, r, 0, 360, 40, color)
  rl.draw_circle_v(rl.Vector2(cx, cy), r * 0.26, color)
  for ang in (180, 0, 90):
    a = math.radians(ang)
    rl.draw_line_ex(rl.Vector2(cx, cy), rl.Vector2(cx + math.cos(a) * (r - 2), cy + math.sin(a) * (r - 2)), th, color)


def vignette(rect: rl.Rectangle, color: rl.Color, strength: float = 0.6, size: float = 170) -> None:
  a = alpha(color, 170 * strength)
  z = alpha(color, 0)
  x, y, w, h = int(rect.x), int(rect.y), int(rect.width), int(rect.height)
  s = int(size)
  rl.draw_rectangle_gradient_v(x, y, w, s, a, z)
  rl.draw_rectangle_gradient_v(x, y + h - s, w, s, z, a)
  rl.draw_rectangle_gradient_h(x, y, s, h, a, z)
  rl.draw_rectangle_gradient_h(x + w - s, y, s, h, z, a)
