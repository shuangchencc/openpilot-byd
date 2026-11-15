"""Song PLUS HUD: bottom glass speed bar, top-right data panel, blind-spot badges.

State (speed, set speed, units) comes from HudRenderer._update_state; only the drawing differs.
"""
import pyray as rl

from openpilot.common.constants import CV
from openpilot.selfdrive.ui.onroad import ar_style as S
from openpilot.selfdrive.ui.onroad.hud_renderer import HudRenderer, CRUISE_DISABLED_CHAR
from openpilot.selfdrive.ui.ui_state import ui_state

BAR_W, BAR_H, BAR_MARGIN = 760, 150, 36
PANEL_W, PANEL_ROW = 264, 74

BAR_STATE = {
  S.Mode.ENGAGED: (S.TEAL, "辅助驾驶中"),
  S.Mode.OVERRIDE: (S.GREY, "驾驶员覆盖"),
  S.Mode.OFF: (S.GREY, "未开启"),
  S.Mode.ALERT: (S.RED, "请接管"),
}


class ARHudRenderer(HudRenderer):
  def _render(self, rect: rl.Rectangle) -> None:
    mode = S.current_mode()
    # soft shade under the bar so it reads on a bright road
    rl.draw_rectangle_gradient_v(int(rect.x), int(rect.y + rect.height - 280), int(rect.width), 280,
                                 rl.Color(0, 0, 0, 0), rl.Color(0, 0, 0, 150))
    self._draw_blindspots(rect, mode)
    self._draw_panel(rect)
    self._draw_bar(rect, mode)

  # ---------------------------------------------------------------- bottom bar
  def _draw_bar(self, rect: rl.Rectangle, mode: S.Mode) -> None:
    colour, label = BAR_STATE[mode]
    x = rect.x + rect.width / 2 - BAR_W / 2
    y = rect.y + rect.height - BAR_H - BAR_MARGIN
    S.glass(x, y, BAR_W, BAR_H, 44, 175)
    if mode == S.Mode.ALERT:
      S.rounded_lines(x, y, BAR_W, BAR_H, 44, 3, S.alpha(S.RED, 220))

    S.wheel_icon(x + 92, y + BAR_H / 2 - 10, 36, colour)
    S.draw_text(label, x + 92, y + BAR_H - 42, 22, colour, self._font_semi_bold, anchor="c")

    divider = rl.Color(255, 255, 255, 50)
    rl.draw_rectangle(int(x + 190), int(y + 30), 2, BAR_H - 60, divider)
    rl.draw_rectangle(int(x + BAR_W - 190), int(y + 30), 2, BAR_H - 60, divider)

    S.draw_text(str(round(self.speed)), x + BAR_W / 2, y + 10, 92, S.WHITE, self._font_bold, anchor="c")
    unit = "km/h" if ui_state.is_metric else "mph"
    S.draw_text(unit, x + BAR_W / 2, y + BAR_H - 36, 22, S.GREY, self._font_medium, anchor="c")

    S.draw_text("设定", x + BAR_W - 95, y + 28, 22, S.GREY, self._font_semi_bold, anchor="c")
    set_text = str(round(self.set_speed)) if self.is_cruise_set else CRUISE_DISABLED_CHAR
    set_colour = S.TEAL if (self.is_cruise_set and mode == S.Mode.ENGAGED) else S.GREY
    S.draw_text(set_text, x + BAR_W - 95, y + 56, 52, set_colour, self._font_bold, anchor="c")

  # ---------------------------------------------------------------- data panel
  def _draw_panel(self, rect: rl.Rectangle) -> None:
    sm = ui_state.sm
    cs = sm['carState']
    lead = sm['radarState'].leadOne if sm.valid['radarState'] else None
    has_lead = lead is not None and lead.present
    conv = CV.MS_TO_KPH if ui_state.is_metric else CV.MS_TO_MPH
    unit = "km/h" if ui_state.is_metric else "mph"
    rows = (
      ("方向盘", f"{cs.steeringAngleDeg:.1f}°"),
      ("加速度", f"{cs.aEgo:+.2f} m/s²"),
      ("前车车速", f"{lead.vLead * conv:.0f} {unit}" if has_lead else "–"),
      ("车距时间", f"{lead.dRel / max(1.0, cs.vEgo):.1f} s" if has_lead else "–"),
    )
    x = rect.x + rect.width - PANEL_W - 36
    y = rect.y + 36
    S.glass(x, y, PANEL_W, 40 + len(rows) * PANEL_ROW, 28, 160)
    for i, (key, val) in enumerate(rows):
      yy = y + 20 + i * PANEL_ROW
      S.draw_text(key, x + 26, yy, 21, S.GREY, self._font_semi_bold)
      S.draw_text(val, x + 26, yy + 26, 34, S.WHITE, self._font_bold)

  # ---------------------------------------------------------------- blind spot badges
  def _draw_blindspots(self, rect: rl.Rectangle, mode: S.Mode) -> None:
    if mode == S.Mode.ALERT:
      return
    cs = ui_state.sm['carState']
    yb = rect.y + rect.height / 2 + 40
    for on, x in ((cs.leftBlindspot, rect.x + 40), (cs.rightBlindspot, rect.x + rect.width - 160)):
      if not on:
        continue
      S.rounded(x, yb - 70, 120, 140, 30, S.alpha(S.RED, 225))
      S.draw_text("盲区", x + 60, yb - 54, 30, S.WHITE, self._font_bold, anchor="c")
      S.draw_text("有车", x + 60, yb - 12, 30, S.WHITE, self._font_bold, anchor="c")
