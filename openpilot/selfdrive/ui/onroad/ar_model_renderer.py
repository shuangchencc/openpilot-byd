"""AR overlays projected onto the road camera image (Song PLUS look).

Same projection and model data as ModelRenderer; only what is drawn differs:
- engaged: teal translucent "carpet" over the planned path with glowing edges, fading out before the lead
- not engaged: a thin grey centre line
- critical alert: carpet removed, lane lines red
- lead (radarState.leadOne, drawn whether or not openpilot controls longitudinal): glowing bar under the car,
  coloured by distance/closing speed, with its distance above it
- blind spot (carState.left/rightBlindspot): the neighbouring lane tinted red
"""
import numpy as np
import pyray as rl

from openpilot.selfdrive.ui.onroad.model_renderer import ModelRenderer, MIN_DRAW_DISTANCE, MAX_DRAW_DISTANCE
from openpilot.selfdrive.ui.onroad import ar_style as S
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.shader_polygon import draw_polygon, Gradient

CARPET_HALF_WIDTH = 0.95   # m
EDGE_HALF_WIDTH = 0.035    # m, crisp edge line
EDGE_GLOW_HALF_WIDTH = 0.09
BLINDSPOT_RANGE = 45.0     # m
LANE_LINE_RANGE = 70.0     # m


def lead_colour(d_rel: float, v_rel: float, v_ego: float) -> rl.Color:
  ttc = d_rel / -v_rel if v_rel < -1.0 else float('inf')
  if d_rel < max(8.0, v_ego * 0.8) or ttc < 3.0:
    return S.RED
  if d_rel < v_ego * 1.4 or ttc < 6.0:
    return S.AMBER
  return S.TEAL


class ARModelRenderer(ModelRenderer):
  def __init__(self):
    super().__init__()
    self._mode = S.Mode.OFF
    self._edges: list[np.ndarray] = []
    self._edge_glows: list[np.ndarray] = []
    self._centre = np.empty((0, 2), dtype=np.float32)
    self._blindspot = [np.empty((0, 2), dtype=np.float32), np.empty((0, 2), dtype=np.float32)]
    self._lead = None  # (x_left, x_right, y_base, y_top, d_rel, colour)
    self._font = gui_app.font(FontWeight.SEMI_BOLD)

  # ---------------------------------------------------------------- update
  def _update_model(self, lead, path_x_array):
    super()._update_model(lead, path_x_array)
    end = self._path_end(lead, path_x_array)
    max_idx = self._get_path_length_idx(path_x_array, end)
    raw = self._path.raw_points
    # the carpet is wider than the stock path
    self._path.projected_points = self._map_line_to_polygon(raw, CARPET_HALF_WIDTH, self._path_offset_z, max_idx, end, allow_invert=False)
    self._edges, self._edge_glows = [], []
    for side in (-CARPET_HALF_WIDTH, CARPET_HALF_WIDTH):
      shifted = raw + np.array([0.0, side, 0.0], dtype=np.float32)
      self._edges.append(self._map_line_to_polygon(shifted, EDGE_HALF_WIDTH, self._path_offset_z, max_idx, end, allow_invert=False))
      self._edge_glows.append(self._map_line_to_polygon(shifted, EDGE_GLOW_HALF_WIDTH, self._path_offset_z, max_idx, end, allow_invert=False))
    self._centre = self._map_line_to_polygon(raw, 0.05, self._path_offset_z, max_idx, end, allow_invert=False)

    # neighbouring lanes for the blind-spot tint: between lane lines 0-1 (left) and 2-3 (right)
    for k, (a, b) in enumerate(((0, 1), (2, 3))):
      self._blindspot[k] = self._lane_strip(self._lane_lines[a].raw_points, self._lane_lines[b].raw_points, BLINDSPOT_RANGE)

    self._update_lead_marker(lead)

  @staticmethod
  def _path_end(lead, path_x_array) -> float:
    # same rule as ModelRenderer._update_model: stop short of the lead
    max_distance = float(np.clip(path_x_array[-1], MIN_DRAW_DISTANCE, MAX_DRAW_DISTANCE))
    if lead is not None and lead.present:
      lead_d = lead.dRel * 2.0
      max_distance = float(np.clip(lead_d - min(lead_d * 0.35, 10.0), 0.0, max_distance))
    return max_distance

  def _lane_strip(self, a: np.ndarray, b: np.ndarray, max_x: float) -> np.ndarray:
    if a.shape[0] == 0 or b.shape[0] == 0:
      return np.empty((0, 2), dtype=np.float32)
    ia = self._get_path_length_idx(a[:, 0], max_x)
    ib = self._get_path_length_idx(b[:, 0], max_x)
    n = min(ia, ib) + 1
    pa = self._project(a[:n])
    pb = self._project(b[:n])
    m = min(len(pa), len(pb))
    if m < 2:
      return np.empty((0, 2), dtype=np.float32)
    return np.vstack((pa[:m], pb[:m][::-1])).astype(np.float32)

  def _project(self, pts: np.ndarray) -> np.ndarray:
    pts = pts[pts[:, 0] >= 0]
    if pts.shape[0] == 0:
      return np.empty((0, 2), dtype=np.float32)
    proj = self._car_space_transform @ pts.T
    ok = np.abs(proj[2]) > 1e-6
    return (proj[:2, ok] / proj[2, ok]).T

  def _update_lead_marker(self, lead):
    self._lead = None
    if lead is None or not lead.present:
      return
    d, y = lead.dRel, -lead.yRel
    idx = self._get_path_length_idx(self._path.raw_points[:, 0], d)
    z = (self._path.raw_points[idx, 2] if idx < len(self._path.raw_points) else 0.0) + self._path_offset_z
    left = self._map_to_screen(d + 1.5, y - 0.95, z)
    right = self._map_to_screen(d + 1.5, y + 0.95, z)
    top = self._map_to_screen(d + 1.5, y, z - 1.6)
    if left and right and top:
      v_ego = ui_state.sm['carState'].vEgo
      self._lead = (left[0], right[0], (left[1] + right[1]) / 2, top[1], d, lead_colour(d, lead.vRel, v_ego))

  # ---------------------------------------------------------------- draw
  def _render(self, rect: rl.Rectangle):
    self._mode = S.current_mode()
    super()._render(rect)
    if self._lead is not None and ui_state.sm.recv_frame['modelV2'] >= ui_state.started_frame:
      self._draw_lead()

  def _vertical_fade(self, poly: np.ndarray, color: rl.Color, near_alpha: float, far_alpha: float = 0.0) -> Gradient:
    """Fade from the near (bottom) end of a projected strip to its far end.

    The polygon shader evaluates the gradient on gl_FragCoord, whose y grows upwards, and the gradient's
    t is 0 at `end`; so `end` is the near end expressed bottom-up (see STOCK_PATH_GRADIENT).
    """
    r = self._rect
    ys = np.clip(poly[:, 1], r.y, r.y + r.height)  # strips start far below the screen; fade over the visible part
    near_up = 1.0 - (float(ys.max()) - r.y) / r.height
    far_up = 1.0 - (float(ys.min()) - r.y) / r.height
    return Gradient(start=(0.0, far_up), end=(0.0, near_up),
                    colors=[S.alpha(color, near_alpha), S.alpha(color, near_alpha * 0.6), S.alpha(color, far_alpha)],
                    stops=[0.0, 0.55, 1.0])

  def _draw_lane_lines(self):
    sm = ui_state.sm
    cs = sm['carState']
    # blind-spot tint first, under everything else
    if self._mode != S.Mode.ALERT:
      for k, on in enumerate((cs.leftBlindspot, cs.rightBlindspot)):
        poly = self._blindspot[k]
        if on and poly.size:
          draw_polygon(self._rect, poly, gradient=self._vertical_fade(poly, S.RED, 90))

    colour = S.RED if self._mode == S.Mode.ALERT else (S.TEAL_LIGHT if self._mode in (S.Mode.ENGAGED, S.Mode.OVERRIDE) else S.WHITE)
    base_alpha = 230 if self._mode == S.Mode.ALERT else 150
    for i in (1, 2):  # own lane only; outer lines add clutter on a camera image
      line = self._lane_lines[i].projected_points
      if line.size == 0:
        continue
      a = base_alpha * float(np.clip(self._lane_line_probs[i] * 1.2, 0.0, 1.0))
      draw_polygon(self._rect, line, gradient=self._vertical_fade(line, colour, a))

  def _draw_path(self):
    if self._mode == S.Mode.ALERT or not self._path.projected_points.size:
      return
    if self._mode == S.Mode.OFF:
      if self._centre.size:
        draw_polygon(self._rect, self._centre, gradient=self._vertical_fade(self._centre, S.PATH_GREY, 200))
      return
    carpet = self._path.projected_points
    draw_polygon(self._rect, carpet, gradient=self._vertical_fade(carpet, S.TEAL, 62))
    for glow, edge in zip(self._edge_glows, self._edges, strict=True):
      if glow.size:
        draw_polygon(self._rect, glow, gradient=self._vertical_fade(glow, S.TEAL, 55))
      if edge.size:
        draw_polygon(self._rect, edge, gradient=self._vertical_fade(edge, S.TEAL, 255, 40))

  def _draw_lead_indicator(self):
    pass  # replaced by _draw_lead, which does not depend on openpilot longitudinal

  def _draw_lead(self):
    x_l, x_r, y_base, y_top, d, colour = self._lead
    if self._mode == S.Mode.ALERT:
      colour = S.RED
    elif self._mode == S.Mode.OFF:
      colour = S.PATH_GREY
    w = (x_r - x_l) * 1.25
    cx = (x_l + x_r) / 2
    by = y_base + 4
    for k, a in ((2.6, 45), (1.7, 80)):
      rl.draw_ellipse(int(cx), int(by), w / 2 * (1 + k * 0.15), 7 * k, S.alpha(colour, a))
    S.rounded(cx - w / 2, by - 4, w, 8, 4, colour)
    label = f"{d:.0f} m"
    lw = S.text_width(label, 30, self._font) + 36
    S.glass(cx - lw / 2, y_top - 62, lw, 48, 24, 170)
    S.draw_text(label, cx, y_top - 60, 30, S.WHITE, self._font, anchor="c")
