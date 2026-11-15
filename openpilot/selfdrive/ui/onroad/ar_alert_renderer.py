"""Alerts as a top banner (the bottom of the screen belongs to the speed bar).

small / mid alerts -> rounded banner at the top centre, coloured by status;
critical alerts additionally get a red edge vignette (the take-over look);
full-screen alerts keep the stock rendering.
Text is drawn with gui_app's fonts so translated alerts keep working.
"""
import pyray as rl

from openpilot.selfdrive.ui.onroad import ar_style as S
from openpilot.selfdrive.ui.onroad.alert_renderer import AlertRenderer
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.text_measure import measure_text_cached

BANNER_W = 900
BANNER_TOP = 36
BANNER_COLOURS = {
  S.AlertStatus.normal: rl.Color(0x14, 0x16, 0x1A, 230),
  S.AlertStatus.userPrompt: S.AMBER_BANNER,
  S.AlertStatus.critical: S.RED_BANNER,
}
TITLE_SIZE, BODY_SIZE = 46, 30


class ARAlertRenderer(AlertRenderer):
  def _render(self, rect: rl.Rectangle):
    alert = self.get_alert(ui_state.sm)
    if not alert:
      return
    if alert.size == S.AlertSize.full:
      super()._render(rect)
      return

    if alert.status == S.AlertStatus.critical:
      S.vignette(rect, S.RED)

    two_lines = alert.size == S.AlertSize.mid and bool(alert.text2)
    h = 128 if two_lines else 92
    x = rect.x + rect.width / 2 - BANNER_W / 2
    y = rect.y + BANNER_TOP
    S.rounded(x, y, BANNER_W, h, 34, BANNER_COLOURS.get(alert.status, BANNER_COLOURS[S.AlertStatus.normal]))
    if alert.status == S.AlertStatus.normal:
      S.rounded_lines(x, y, BANNER_W, h, 34, 1.5, S.GLASS_EDGE)
    else:
      S.wheel_icon(x + 74, y + h / 2, 34, S.WHITE)

    cx = x + BANNER_W / 2 + (30 if alert.status != S.AlertStatus.normal else 0)
    title_y = y + (14 if two_lines else (h - TITLE_SIZE) / 2)
    self._centered(alert.text1, cx, title_y, self.font_bold, TITLE_SIZE, S.WHITE)
    if two_lines:
      self._centered(alert.text2, cx, y + 76, self.font_regular, BODY_SIZE, rl.Color(255, 230, 225, 255))

  @staticmethod
  def _centered(text: str, cx: float, y: float, font: rl.Font, size: int, colour: rl.Color) -> None:
    w = measure_text_cached(font, text, size).x
    rl.draw_text_ex(font, text, rl.Vector2(cx - w / 2, y), size, 0, colour)
