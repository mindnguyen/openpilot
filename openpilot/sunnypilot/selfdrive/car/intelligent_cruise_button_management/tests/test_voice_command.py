"""ICBM + voice: "Hi Comma, giam ve 50" behaves like a driver +/- press that lands on the target."""
from types import SimpleNamespace

from opendbc.car.structs import car
from openpilot.cereal import custom
from openpilot.common.constants import CV
from openpilot.sunnypilot.selfdrive.car.intelligent_cruise_button_management.controller import (
  IntelligentCruiseButtonManagement, State,
)

ButtonType = car.CarState.ButtonEvent.Type


def car_state(set_kph: float, enabled: bool = True, buttons=()):
  return car.CarState(cruiseState={"enabled": enabled, "available": True, "speedCluster": set_kph * CV.KPH_TO_MS},
                      buttonEvents=[car.CarState.ButtonEvent(type=b, pressed=True) for b in buttons])


def plan(limit_kph: float | None = None, curve_kph: float | None = None):
  lp = custom.LongitudinalPlanSP.new_message()
  if limit_kph is not None:
    lp.speedLimit.resolver.speedLimitValid = True
    lp.speedLimit.resolver.speedLimitFinal = limit_kph * CV.KPH_TO_MS
  if curve_kph is not None:
    lp.smartCruiseControl.vision.active = True
    lp.smartCruiseControl.vision.vTarget = curve_kph * CV.KPH_TO_MS
  return lp


def voice(target_kph: float, accepted: bool = True):
  vc = custom.VoiceCommand.new_message()
  vc.accepted = accepted
  vc.targetSpeed = target_kph
  vc.isMetric = True
  return vc


CC = car.CarControl(enabled=True)


def make_icbm():
  icbm = IntelligentCruiseButtonManagement(SimpleNamespace(), SimpleNamespace(pcmCruiseSpeed=False))
  icbm.run(car_state(80), CC, plan(), True)  # first frame sets is_metric
  return icbm


def test_voice_sets_target_and_icbm_decreases():
  icbm = make_icbm()
  assert icbm.apply_voice_command(voice(50), car_state(80), plan())
  for _ in range(100):  # 1 s: past the pre-active timer
    icbm.run(car_state(80), CC, plan(), True)
  assert icbm.v_target == 50
  assert icbm.state == State.decreasing


def test_voice_holds_against_same_sign_until_a_new_one():
  icbm = make_icbm()
  icbm.run(car_state(80), CC, plan(limit_kph=80), True)
  assert icbm.apply_voice_command(voice(50), car_state(80), plan(limit_kph=80))
  icbm.run(car_state(80), CC, plan(limit_kph=80), True)
  assert icbm.v_target == 50          # same sign: the voice target wins
  icbm.run(car_state(50), CC, plan(limit_kph=60), True)
  assert icbm.v_target == 60          # a new sign: follow the limit again


def test_curve_still_lowers_the_voice_target():
  icbm = make_icbm()
  icbm.apply_voice_command(voice(70), car_state(80), plan())
  icbm.run(car_state(80), CC, plan(curve_kph=45), True)
  assert icbm.v_target == 45
  icbm.run(car_state(45), CC, plan(), True)
  assert icbm.v_target == 70          # back to the voice speed after the curve


def test_driver_button_wins_over_voice():
  icbm = make_icbm()
  icbm.apply_voice_command(voice(50), car_state(80), plan())
  icbm.run(car_state(80, buttons=[ButtonType.accelCruise]), CC, plan(), True)
  for _ in range(6):                  # the cluster value is captured a few frames after the press
    icbm.run(car_state(81), CC, plan(), True)
  assert icbm.v_target == 81


def test_rejected_commands():
  icbm = make_icbm()
  assert not icbm.apply_voice_command(voice(50, accepted=False), car_state(80), plan())
  assert not icbm.apply_voice_command(voice(50), car_state(80, enabled=False), plan())
  icbm_pcm = IntelligentCruiseButtonManagement(SimpleNamespace(), SimpleNamespace(pcmCruiseSpeed=True))
  assert not icbm_pcm.apply_voice_command(voice(50), car_state(80), plan())


def test_voice_below_minimum_is_clamped():
  icbm = make_icbm()
  assert icbm.apply_voice_command(voice(20), car_state(80), plan())
  icbm.run(car_state(80), CC, plan(), True)
  assert icbm.v_target == 30
