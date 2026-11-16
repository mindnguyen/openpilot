"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from openpilot.cereal import messaging, custom

from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP

GREEN_LIGHT_X_THRESHOLD = 30
LEAD_DEPART_DIST_THRESHOLD = 1.0
TRIGGER_TIMER_THRESHOLD = 0.3

# Teramont X fork: green-light go.  With openpilot longitudinal the car holds at a stop line until the model's
# short-horizon plan says go, which lags the green light when nothing is in front.  When the model's path already
# reaches far ahead while we are still holding, request a short gentle start (the same effect as a tap on the
# gas) and then hand back to the model.  Only the model's stop is overridden: the lead and set-speed limits in
# the planner still apply.  Off together with the GreenLightAlert toggle.
GREEN_LIGHT_GO = True
GREEN_LIGHT_GO_X_THRESHOLD = 30.     # m, model path length at the horizon that counts as "wants to drive"
GREEN_LIGHT_GO_TRIGGER_TIME = 0.5    # s the path has to stay that long
GREEN_LIGHT_GO_MIN_STAND_TIME = 2.0  # s at standstill before a go is considered
GREEN_LIGHT_GO_MIN_LEAD_DIST = 6.0   # m, a lead closer than this blocks the go (lead following handles that case)
GREEN_LIGHT_GO_ACCEL = 0.6           # m/s^2 requested while the go is active
GREEN_LIGHT_GO_MAX_TIME = 2.0        # s
GREEN_LIGHT_GO_END_SPEED = 1.0       # m/s, rolling: the model takes over again
GREEN_LIGHT_GO_ABORT_X = 15.         # m, the go is dropped if the model path shrinks below this again


class E2EStates:
  INACTIVE = 0
  ARMED = 1
  CONSUMED = 2


class E2EAlertsHelper:
  def __init__(self):
    self._params = Params()
    self.frame = -1
    self.green_light_state = E2EStates.INACTIVE
    self.prev_green_light_state = E2EStates.INACTIVE
    self.lead_depart_state = E2EStates.INACTIVE
    self.prev_lead_depart_state = E2EStates.INACTIVE

    self.green_light_alert = False
    self.green_light_alert_enabled = self._params.get_bool("GreenLightAlert")
    self.lead_depart_alert = False
    self.lead_depart_alert_enabled = self._params.get_bool("LeadDepartAlert")

    self.green_light_trigger_timer = 0
    self.lead_depart_trigger_timer = 0
    self.last_lead_distance = -1
    self.last_moving_frame = -1

    self.allowed = False
    self.last_allowed = False
    self.has_lead = False

    self.lead_depart_arm_timer = 0
    self.lead_depart_confirmed_lead = False
    self.lead_depart_armed = False

    # green-light go (see GREEN_LIGHT_GO)
    self.op_long = False          # set by the planner: only meaningful when openpilot controls gas and brake
    self.green_light_go = False
    self.lead_d_rel = 0.
    self.go_stand_frames = 0
    self.go_path_frames = 0
    self.go_frames_left = 0
    self.go_consumed = False

  def _read_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.green_light_alert_enabled = self._params.get_bool("GreenLightAlert")
      self.lead_depart_alert_enabled = self._params.get_bool("LeadDepartAlert")

  def update_alert_trigger(self, sm: messaging.SubMaster):
    CS = sm['carState']
    CC = sm['carControl']

    model_x = sm['modelV2'].position.x
    max_idx = len(model_x) - 1
    self.has_lead = sm['radarState'].leadOne.present
    lead_dRel = sm['radarState'].leadOne.dRel
    self.lead_d_rel = lead_dRel

    standstill = CS.standstill
    moving = not standstill and CS.vEgo > 0.1

    if moving:
      self.last_moving_frame = self.frame
    recent_moving = self.last_moving_frame == -1 or (self.frame - self.last_moving_frame) * DT_MDL < 2.0

    self.allowed = not moving and not CS.gasPressed and not CC.enabled and not recent_moving

    # Green Light Alert
    green_light_trigger = False
    if self.green_light_state == E2EStates.ARMED:
      if model_x[max_idx] > GREEN_LIGHT_X_THRESHOLD:
        self.green_light_trigger_timer += 1
      else:
        self.green_light_trigger_timer = 0

      if self.green_light_trigger_timer * DT_MDL > TRIGGER_TIMER_THRESHOLD:
        green_light_trigger = True
    elif self.green_light_state != E2EStates.ARMED:
      self.green_light_trigger_timer = 0

    # Lead Departure Alert
    close_lead_valid = self.has_lead and lead_dRel < 8.0
    if self.allowed and not self.last_allowed and close_lead_valid:
      self.lead_depart_confirmed_lead = True
    elif not self.allowed:
      self.lead_depart_confirmed_lead = False

    if self.allowed and self.lead_depart_confirmed_lead and close_lead_valid:
      self.lead_depart_arm_timer += 1

      if self.lead_depart_arm_timer * DT_MDL >= 1.0:
        self.lead_depart_armed = True
    else:
      self.lead_depart_arm_timer = 0
      self.lead_depart_armed = False

    lead_depart_trigger = False
    if self.lead_depart_state == E2EStates.ARMED:
      if self.last_lead_distance == -1 or lead_dRel < self.last_lead_distance:
        self.last_lead_distance = lead_dRel

      if self.last_lead_distance != -1 and (lead_dRel - self.last_lead_distance > LEAD_DEPART_DIST_THRESHOLD):
        self.lead_depart_trigger_timer += 1
      else:
        self.lead_depart_trigger_timer = 0

      if self.lead_depart_trigger_timer * DT_MDL > TRIGGER_TIMER_THRESHOLD:
        lead_depart_trigger = True
    elif self.lead_depart_state != E2EStates.ARMED:
      self.last_lead_distance = -1
      self.lead_depart_trigger_timer = 0

    self.last_allowed = self.allowed

    return green_light_trigger, lead_depart_trigger

  def update_green_light_go(self, sm: messaging.SubMaster, events_sp: EventsSP) -> None:
    CS = sm['carState']
    CC = sm['carControl']
    model_x = sm['modelV2'].position.x
    path_len = model_x[len(model_x) - 1] if len(model_x) else 0.

    engaged = self.op_long and CC.enabled and CC.longActive
    holding = engaged and CS.standstill and not CS.gasPressed and not CS.brakePressed
    rolling = not CS.standstill and CS.vEgo > GREEN_LIGHT_GO_END_SPEED

    if holding:
      self.go_stand_frames += 1
      self.go_path_frames = self.go_path_frames + 1 if path_len > GREEN_LIGHT_GO_X_THRESHOLD else 0
      lead_close = self.has_lead and self.lead_d_rel < GREEN_LIGHT_GO_MIN_LEAD_DIST
      if (GREEN_LIGHT_GO and self.green_light_alert_enabled and not self.go_consumed and not lead_close and
          self.go_stand_frames * DT_MDL >= GREEN_LIGHT_GO_MIN_STAND_TIME and
          self.go_path_frames * DT_MDL >= GREEN_LIGHT_GO_TRIGGER_TIME):
        self.go_consumed = True   # one go per stop
        self.go_frames_left = int(GREEN_LIGHT_GO_MAX_TIME / DT_MDL)
        events_sp.add(custom.OnroadEventSP.EventName.e2eChime)
    else:
      self.go_stand_frames = 0
      self.go_path_frames = 0
      if rolling:
        self.go_consumed = False

    # the go ends when it has done its job (rolling), when the driver brakes, when openpilot is no longer driving,
    # or when the model takes back its long path (a false trigger)
    if not engaged or CS.brakePressed or rolling or path_len < GREEN_LIGHT_GO_ABORT_X:
      self.go_frames_left = 0
    self.green_light_go = self.go_frames_left > 0
    self.go_frames_left = max(0, self.go_frames_left - 1)

  @staticmethod
  def update_state_machine(state: int, enabled: bool, allowed: bool, triggered: bool) -> tuple[int, bool]:
    if state != E2EStates.INACTIVE:
      if not allowed or not enabled:
        state = E2EStates.INACTIVE

      else:
        if state == E2EStates.ARMED:
          if triggered:
            state = E2EStates.CONSUMED

        elif state == E2EStates.CONSUMED:
          pass

    elif state == E2EStates.INACTIVE:
      if allowed and enabled:
        state = E2EStates.ARMED

    return state, triggered

  def update(self, sm: messaging.SubMaster, events_sp: EventsSP) -> None:
    self._read_params()

    green_light_trigger, lead_depart_trigger = self.update_alert_trigger(sm)

    self.prev_green_light_state = self.green_light_state
    self.prev_lead_depart_state = self.lead_depart_state

    self.green_light_state, self.green_light_alert = self.update_state_machine(
      self.green_light_state,
      self.green_light_alert_enabled,
      self.allowed and not self.has_lead,
      green_light_trigger
    )

    self.lead_depart_state, self.lead_depart_alert = self.update_state_machine(
      self.lead_depart_state,
      self.lead_depart_alert_enabled,
      self.allowed and self.lead_depart_armed,
      lead_depart_trigger
    )

    if self.green_light_alert or self.lead_depart_alert:
      events_sp.add(custom.OnroadEventSP.EventName.e2eChime)

    self.update_green_light_go(sm, events_sp)

    self.frame += 1
