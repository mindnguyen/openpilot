"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.cereal import custom
from opendbc.car.structs import car
from opendbc.car import structs, apply_hysteresis
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_CTRL
from openpilot.sunnypilot.selfdrive.car.intelligent_cruise_button_management.helpers import get_minimum_set_speed
from openpilot.sunnypilot.selfdrive.car.cruise_ext import CRUISE_BUTTON_TIMER, update_manual_button_timers

LongitudinalPlanSource = custom.LongitudinalPlanSP.LongitudinalPlanSource
State = custom.IntelligentCruiseButtonManagement.IntelligentCruiseButtonManagementState
SendButtonState = custom.IntelligentCruiseButtonManagement.SendButtonState

ALLOWED_SPEED_THRESHOLD = 1.8  # m/s, ~4 MPH
HYST_GAP = 0.0  # currently disabled; TODO-SP: might need to be brand-specific
INACTIVE_TIMER = 0.4


SEND_BUTTONS = {
  State.increasing: SendButtonState.increase,
  State.decreasing: SendButtonState.decrease,
}


class IntelligentCruiseButtonManagement:
  def __init__(self, CP: structs.CarParams, CP_SP: structs.CarParamsSP):
    self.CP = CP
    self.CP_SP = CP_SP

    self.v_target = 0
    self.v_cruise_cluster = 0
    self.v_cruise_min = 0
    self.cruise_button = SendButtonState.none
    self.state = State.inactive
    self.pre_active_timer = 0

    self.is_ready = False
    self.is_ready_prev = False
    self.v_target_ms_last = 0.0
    self.is_metric = False

    # driver override: a deliberate +/- press past the limit while engaged holds the driver's speed until a
    # different limit shows up.  SET/RESUME (engaging) and presses towards the limit never arm it.
    self.driver_override = False
    self.override_limit_ms: float | None = None
    self.driver_set_ms = 0.0
    self._capture_set_frames = 0
    self._pending_press: tuple[int, float | None] | None = None
    self.cruise_enabled_prev = False

    self.cruise_button_timers = CRUISE_BUTTON_TIMER

  @property
  def v_cruise_equal(self) -> bool:
    return self.v_target == self.v_cruise_cluster

  @staticmethod
  def resolved_limit(LP_SP: custom.LongitudinalPlanSP) -> float | None:
    res = LP_SP.speedLimit.resolver
    return float(res.speedLimitFinal) if (res.speedLimitValid and res.speedLimitFinal > 0.) else None

  @staticmethod
  def curve_targets(LP_SP: custom.LongitudinalPlanSP) -> list[float]:
    """Smart cruise control vision/map targets, only while they are actively slowing the car."""
    scc = LP_SP.smartCruiseControl
    out = []
    if scc.vision.active and 0. < scc.vision.vTarget < 100.:
      out.append(float(scc.vision.vTarget))
    if scc.map.active and 0. < scc.map.vTarget < 100.:
      out.append(float(scc.map.vTarget))
    return out

  @classmethod
  def speed_limit_target(cls, LP_SP: custom.LongitudinalPlanSP) -> float | None:
    """Plain limit-following target (no driver override), in m/s, or None when nothing should move the set speed.

    With stock ACC the generic plan target (LP_SP.vTarget) tracks the current speed, so following it
    drags the set speed down to the minimum.  Only use sources that mean something for a set speed:
    the resolved speed limit (car sign camera / map, with the user's offset) and the smart cruise
    control curve targets while they are actively slowing the car.
    """
    cands = cls.curve_targets(LP_SP)
    limit = cls.resolved_limit(LP_SP)
    if limit is not None:
      cands.append(limit)
    return min(cands) if cands else None

  def update_driver_override(self, CS: car.CarState, limit_ms: float | None) -> None:
    """The driver's +/- press past the (possibly misread) sign wins until a different limit appears.

    Only presses made while cruise was already engaged count (SET/RESUME engage the car's ACC and must not be
    mistaken for a speed choice), and only when the resulting set speed went beyond the limit in the pressed
    direction; a press that merely moves towards the limit lets ICBM keep going.  Logged 2026-10-30: the old
    rule latched the engage SET at 29 km/h and then fought the driver's RESUME presses.
    """
    enabled = bool(CS.cruiseState.enabled)
    if not enabled:
      if self.cruise_enabled_prev:  # disengaged: next engagement starts fresh
        self.driver_override = False
        self._pending_press = None
        self._capture_set_frames = 0
      self.cruise_enabled_prev = False
      return
    if self.cruise_enabled_prev:
      for b in CS.buttonEvents:
        if b.pressed and b.type in (car.CarState.ButtonEvent.Type.accelCruise, car.CarState.ButtonEvent.Type.decelCruise):
          self._pending_press = (1 if b.type == car.CarState.ButtonEvent.Type.accelCruise else -1, limit_ms)
          self._capture_set_frames = 20  # the cluster value lags the press; judge it 0.2 s later
    self.cruise_enabled_prev = True
    if self._pending_press is not None and self._capture_set_frames > 0:
      self._capture_set_frames -= 1
      if self._capture_set_frames == 0:
        direction, lim_at_press = self._pending_press
        self._pending_press = None
        after_ms = float(CS.cruiseState.speedCluster)
        if lim_at_press is None:
          arm = True  # nothing to follow: keep what the driver chose
        elif direction > 0:
          arm = after_ms > lim_at_press + 0.3
        else:
          arm = after_ms < lim_at_press - 0.3
        if arm:
          self.driver_override = True
          self.driver_set_ms = after_ms
          self.override_limit_ms = lim_at_press
    if self.driver_override and limit_ms is not None:
      if self.override_limit_ms is None or abs(limit_ms - self.override_limit_ms) > 0.3:
        self.driver_override = False  # a new sign: follow the limit again

  def update_calculations(self, CS: car.CarState, LP_SP: custom.LongitudinalPlanSP) -> None:
    speed_conv = CV.MS_TO_KPH if self.is_metric else CV.MS_TO_MPH
    ms_conv = CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS

    self.v_cruise_min = get_minimum_set_speed(self.is_metric)
    self.v_cruise_cluster = round(CS.cruiseState.speedCluster * speed_conv)

    limit = self.resolved_limit(LP_SP)
    self.update_driver_override(CS, limit)

    cands = self.curve_targets(LP_SP)
    if self.driver_override:
      if self.driver_set_ms > 0.:
        cands.append(self.driver_set_ms)  # base = what the driver chose; curves may still lower it temporarily
    elif limit is not None and self._pending_press is None:
      cands.append(limit)  # while a driver press is being judged, do not pull against it
    target_ms = min(cands) if cands else None

    if target_ms is None:
      # no speed limit and no curve slowdown: leave the driver's set speed alone
      self.v_target_ms_last = CS.cruiseState.speedCluster
      self.v_target = self.v_cruise_cluster
      return

    self.v_target_ms_last = apply_hysteresis(target_ms, self.v_target_ms_last, HYST_GAP * ms_conv)
    self.v_target = max(round(self.v_target_ms_last * speed_conv), self.v_cruise_min)

  def update_state_machine(self) -> custom.IntelligentCruiseButtonManagement.SendButtonState:
    self.pre_active_timer = max(0, self.pre_active_timer - 1)

    # HOLDING, ACCELERATING, DECELERATING, PRE_ACTIVE
    if self.state != State.inactive:
      if not self.is_ready:
        self.state = State.inactive

      else:
        # PRE_ACTIVE
        if self.state == State.preActive:
          if self.pre_active_timer <= 0:
            if self.v_cruise_equal:
              self.state = State.holding

            elif self.v_target > self.v_cruise_cluster:
              self.state = State.increasing

            elif self.v_target < self.v_cruise_cluster and self.v_cruise_cluster > self.v_cruise_min:
              self.state = State.decreasing

        # HOLDING
        elif self.state == State.holding:
          if not self.v_cruise_equal:
            self.state = State.preActive

        # ACCELERATING
        elif self.state == State.increasing:
          if self.v_target <= self.v_cruise_cluster:
            self.state = State.holding

        # DECELERATING
        elif self.state == State.decreasing:
          if self.v_target >= self.v_cruise_cluster or self.v_cruise_cluster <= self.v_cruise_min:
            self.state = State.holding

    # INACTIVE
    elif self.state == State.inactive:
      if self.is_ready and not self.is_ready_prev:
        self.pre_active_timer = int(INACTIVE_TIMER / DT_CTRL)
        self.state = State.preActive

    send_button = SEND_BUTTONS.get(self.state, SendButtonState.none)

    return send_button

  def update_readiness(self, CS: car.CarState, CC: car.CarControl) -> None:
    update_manual_button_timers(CS, self.cruise_button_timers)

    ready = CC.enabled and not CC.cruiseControl.override and not CC.cruiseControl.cancel and not CC.cruiseControl.resume
    button_pressed = any(self.cruise_button_timers[k] > 0 for k in self.cruise_button_timers)

    self.is_ready = ready and not button_pressed

  def run(self, CS: car.CarState, CC: car.CarControl, LP_SP: custom.LongitudinalPlanSP, is_metric: bool) -> None:
    if self.CP_SP.pcmCruiseSpeed:
      return

    self.is_metric = is_metric

    self.update_calculations(CS, LP_SP)
    self.update_readiness(CS, CC)

    self.cruise_button = self.update_state_machine()

    self.is_ready_prev = self.is_ready
