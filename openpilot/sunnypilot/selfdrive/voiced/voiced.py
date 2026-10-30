#!/usr/bin/env python3
"""
voiced: change the ICBM set speed by voice, e.g. "Hi Comma, giảm về 50".

Listens to the microphone stream micd already publishes (rawAudioData, 16 kHz mono), recognizes speech offline
with a small Vietnamese Vosk model restricted to the command vocabulary, and publishes one voiceCommand per
command heard after the wake phrase. selfdrived hands accepted commands to ICBM, which treats them like a
driver +/- press: the new speed holds until a different speed limit sign shows up, curve slowdowns still apply,
and any real button press wins.

Runs only onroad and only while the VoiceCruiseControl param is on. Vosk lives under /data/voice because the
root filesystem is read-only; install it with install_vosk.sh. Without it this process idles instead of
crashing, so the manager does not restart it in a loop.
"""
import json
import os
import sys
import time

from openpilot.cereal import messaging
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.selfdrive.car.intelligent_cruise_button_management.helpers import get_minimum_set_speed
from openpilot.sunnypilot.selfdrive.voiced.commands import CommandSession, Rejected, grammar_words, resolve_target

VOICE_DIR = os.getenv("VOICED_DIR", "/data/voice")
MODEL_DIR = os.getenv("VOICED_MODEL", os.path.join(VOICE_DIR, "vosk-model-small-vn-0.4"))
PYLIB_DIR = os.path.join(VOICE_DIR, "pylib")
SAMPLE_RATE = 16000
MAX_SET_SPEED = {True: 130, False: 80}  # km/h, mph


def load_recognizer():
  # appended, not prepended: never shadow openpilot's own copies of shared packages
  if os.path.isdir(PYLIB_DIR) and PYLIB_DIR not in sys.path:
    sys.path.append(PYLIB_DIR)
  try:
    import vosk
  except ImportError as e:
    cloudlog.error(f"voiced: vosk is not installed ({e}); run install_vosk.sh")
    return None
  if not os.path.isdir(MODEL_DIR):
    cloudlog.error(f"voiced: no model at {MODEL_DIR}; run install_vosk.sh")
    return None

  vosk.SetLogLevel(-1)
  model = vosk.Model(MODEL_DIR)
  grammar = json.dumps(grammar_words() + ["[unk]"], ensure_ascii=False)
  cloudlog.info(f"voiced: model {MODEL_DIR} loaded")
  return vosk.KaldiRecognizer(model, SAMPLE_RATE, grammar)


class VoiceCruise:
  def __init__(self, sm: messaging.SubMaster, pm: messaging.PubMaster, is_metric: bool):
    self.sm = sm
    self.pm = pm
    self.is_metric = is_metric
    self.session = CommandSession()
    self.command_id = 0

  def current_set_speed(self) -> int | None:
    CS = self.sm['carState']
    if not self.sm.seen['carState'] or not CS.cruiseState.enabled or CS.cruiseState.speedCluster <= 0.:
      return None
    conv = CV.MS_TO_KPH if self.is_metric else CV.MS_TO_MPH
    return round(CS.cruiseState.speedCluster * conv)

  def handle_text(self, text: str, now: float) -> None:
    result = self.session.handle(text, now)
    if result is None:
      return

    target, reason = None, ""
    if isinstance(result, Rejected):
      reason = result.reason
    else:
      current = self.current_set_speed()
      if current is None:
        reason = "cruise control is off"
      else:
        target, reason = resolve_target(result, current, get_minimum_set_speed(self.is_metric), MAX_SET_SPEED[self.is_metric])

    self.command_id += 1
    msg = messaging.new_message('voiceCommand', valid=True)
    vc = msg.voiceCommand
    vc.commandId = self.command_id
    vc.accepted = target is not None
    vc.targetSpeed = float(target or 0)
    vc.isMetric = self.is_metric
    vc.text = text
    vc.reason = reason
    self.pm.send('voiceCommand', msg)
    cloudlog.info(f"voiced: heard {text!r} -> target={target} reason={reason!r}")


def main():
  params = Params()
  rec = load_recognizer()
  if rec is None:
    while True:
      time.sleep(60.)

  audio_sock = messaging.sub_sock('rawAudioData', conflate=False)
  sm = messaging.SubMaster(['carState'])
  pm = messaging.PubMaster(['voiceCommand'])
  voice = VoiceCruise(sm, pm, params.get_bool("IsMetric"))

  while True:
    # every audio chunk matters, so drain the socket instead of reading only the latest message
    msgs = messaging.drain_sock(audio_sock, wait_for_one=True)
    sm.update(0)
    for m in msgs:
      if rec.AcceptWaveform(bytes(m.rawAudioData.data)):
        text = json.loads(rec.Result()).get("text", "").strip()
        if text:
          voice.handle_text(text, time.monotonic())


if __name__ == "__main__":
  main()
