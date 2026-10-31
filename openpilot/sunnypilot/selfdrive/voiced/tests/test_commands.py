import pytest

from openpilot.sunnypilot.selfdrive.voiced.commands import (
  CommandSession, Rejected, VoiceCommand, grammar_words, parse_number, resolve_target, tokenize,
)


@pytest.mark.parametrize("words, value", [
  ("50", 50), ("năm mươi", 50), ("tám mươi", 80), ("sáu mươi lăm", 65), ("năm mươi năm", 55),
  ("hai mươi mốt", 21), ("hai mươi tư", 24), ("mười", 10), ("mười lăm", 15), ("năm chục", 50),
  ("một trăm", 100), ("một trăm hai mươi", 120), ("một trăm hai", 120), ("trăm linh năm", 105),
  ("một trăm lẻ năm", 105), ("trăm rưỡi", 150), ("bảy", 7),
  ("tám mười", 80), ("sáu mười lăm", 65),  # how the recognizer returned "tám mươi" on the comma
  ("năm lăm", 55), ("hai mốt", 21), ("ba tư", 34),  # colloquial tens without "mươi"
])
def test_parse_number(words, value):
  assert parse_number(tokenize(words), 0) == (value, len(tokenize(words)))


def cmd(text):
  return CommandSession().handle(text, 0.)


@pytest.mark.parametrize("text, expected", [
  ("Hi Comma, giảm về 50km/h", VoiceCommand("set", 50, "down")),
  ("hai com ma giảm về năm mươi", VoiceCommand("set", 50, "down")),
  ("hai com ma giảm xuống năm mươi ki lô mét một giờ", VoiceCommand("set", 50, "down")),
  ("hai com ma tăng lên tám mươi", VoiceCommand("set", 80, "up")),
  ("hai com ma đặt tốc độ sáu mươi", VoiceCommand("set", 60, None)),
  ("hai com ma bảy mươi", VoiceCommand("set", 70, None)),
  ("hai com ma giảm mười", VoiceCommand("delta", -10, None)),
  ("hai com ma tăng năm", VoiceCommand("delta", 5, None)),
  ("ê hai com ma giảm về sáu mươi nhé", VoiceCommand("set", 60, "down")),
  ("hey comma tăng lên một trăm", VoiceCommand("set", 100, "up")),
  ("hai com ma giảm năm mươi", VoiceCommand("set", 50, "down")),   # "về" dropped: 50 is a target, not -50
  ("hai com ma tăng bốn mươi", VoiceCommand("set", 40, "up")),
  # exactly what Vosk returned on the comma for the TTS test commands (2026-10-31)
  ("hay còn ma giảm về năm mươi", VoiceCommand("set", 50, "down")),
  ("hi com hạ giảm năm mươi ki lô mét trên", VoiceCommand("set", 50, "down")),
  ("hay còn ma tăng lên tám mười", VoiceCommand("set", 80, "up")),
  ("hay còn ma tăng mười", VoiceCommand("delta", 10, None)),
  ("hay còn ma đặt sáu mươi lăm", VoiceCommand("set", 65, None)),
  # ... and what the real voiced process heard from a continuous stream
  ("hay con ma giảm hi năm mươi", VoiceCommand("set", 50, "down")),
  ("hi con ma giảm hi năm mươi ki lô mét trên rưỡi", VoiceCommand("set", 50, "down")),
  ("hay co ma tăng lên tám mươi", VoiceCommand("set", 80, "up")),
  ("hay co ma tăng mười", VoiceCommand("delta", 10, None)),
  ("hay co ma đặt sáu mươi lăm", VoiceCommand("set", 65, None)),
  ("hai cam hạ giảm về sáu mươi", VoiceCommand("set", 60, "down")),
  # real cabin speech, 2026-10-31 recording after auto gain: the "ma" syllable is lost
  ("hai com giảm về năm lăm", VoiceCommand("set", 55, "down")),
  ("hay com tăng lên tám mươi", VoiceCommand("set", 80, "up")),
  ("hai trăm bớt giảm năm lăm mét linh", VoiceCommand("set", 55, "down")),
  ("hai com tốc độ sáu mươi", VoiceCommand("set", 60, None)),
])
def test_commands(text, expected):
  got = cmd(text)
  assert isinstance(got, VoiceCommand)
  assert (got.kind, got.value, got.direction) == (expected.kind, expected.value, expected.direction)


@pytest.mark.parametrize("text", [
  "hai com ma giảm lên năm mươi",   # conflicting words
  "hai com ma đặt năm",             # 5 km/h is a misparse
])
def test_rejected(text):
  assert isinstance(cmd(text), Rejected)


@pytest.mark.parametrize("text", [
  "giảm về năm mươi",               # no wake phrase
  "hai mươi năm rồi",               # normal talk that starts with "hai"
  "radio: tăng lên tám mươi",
  "",
  # what Vosk returned on the comma for ordinary talk (2026-10-31)
  "không hay chỉnh đặt", "đi rưỡi về nhé", "giảm hạ năm mươi trăm số tới hạ set giảm",
  "hay còn gọi là năm mươi",        # "hay còn" is everyday speech; the wake needs "ma" right after it
  "thêm đi lăm số rưỡi về nhé", "giảm hạ năm mươi linh trăm số tới hạ giảm giảm",
  "hai con gà giảm về năm mươi",    # "hai con" + something else
  "hai lăm mét giờ",                # "hai lăm" is only a wake phrase when a command follows
  "giảm về hai lăm",                # 25 inside a sentence is a number, not a wake phrase
])
def test_ignored_without_wake(text):
  assert cmd(text) is None


def test_wake_alone_arms_next_utterance():
  s = CommandSession()
  assert s.handle("hai com ma", 10.) is None
  got = s.handle("giảm về năm mươi", 12.)
  assert got == VoiceCommand("set", 50, "down", "giảm về năm mươi")
  # one command per wake
  assert s.handle("tăng lên tám mươi", 13.) is None


def test_verb_without_number_keeps_listening():
  s = CommandSession()
  assert s.handle("hai com ma giảm", 10.) is None   # the number was not heard: wait for it instead of rejecting
  assert s.handle("giảm về năm lăm", 12.) == VoiceCommand("set", 55, "down", "giảm về năm lăm")
  assert s.handle("hai com lên lên", 20.) is None
  assert s.handle("lên", 21.) is None               # still no number: keep waiting
  assert s.handle("tám mươi", 23.) == VoiceCommand("set", 80, None, "tám mươi")
  assert isinstance(s.handle("hai com ma giảm lên năm mươi", 30.), Rejected)   # other rejections still disarm
  assert s.handle("giảm về năm mươi", 31.) is None


def test_cabin_recording_sequence():
  # what the recognizer returned for the 2026-10-31 cabin recording (auto gain, grammar)
  s = CommandSession()
  assert s.handle("hai lăm về", 46.) is None
  assert s.handle("giảm về năm lăm mét giờ", 48.) == VoiceCommand("set", 55, "down", "giảm về năm lăm mét giờ")
  assert s.handle("hay lăm bớt", 51.) is None
  assert s.handle("giảm năm mét giờ", 53.) == VoiceCommand("delta", -5, None, "giảm năm mét giờ")


def test_arm_times_out():
  s = CommandSession()
  s.handle("hi comma", 10.)
  assert s.handle("giảm về năm mươi", 16.) is None


@pytest.mark.parametrize("command, current, expected", [
  (VoiceCommand("set", 50, "down"), 80, 50),
  (VoiceCommand("set", 20, "down"), 80, 30),     # clamped to the minimum
  (VoiceCommand("set", 90, "down"), 80, None),   # "giảm về 90" while at 80
  (VoiceCommand("set", 60, "up"), 80, None),
  (VoiceCommand("set", 80, None), 80, None),     # already there
  (VoiceCommand("set", 200, None), 80, 130),     # clamped to the maximum
  (VoiceCommand("delta", -10, None), 80, 70),
  (VoiceCommand("delta", 10, None), 125, 130),
])
def test_resolve_target(command, current, expected):
  target, reason = resolve_target(command, current, 30, 130)
  assert target == expected
  assert (reason == "") == (expected is not None)


def test_grammar_has_every_word_the_parser_needs():
  words = set(grammar_words())
  for w in ("hai", "com", "ma", "giảm", "về", "tăng", "lên", "năm", "mươi", "lăm", "trăm", "mười"):
    assert w in words


def test_gain_lifts_and_clips():
  import numpy as np
  from openpilot.sunnypilot.selfdrive.voiced.audio import Gain
  quiet = (np.sin(np.linspace(0, 100, 800)) * 1000).astype(np.int16)
  out = np.frombuffer(Gain().apply(quiet.tobytes()), dtype=np.int16)
  assert 4900 < np.abs(out).max() < 5100          # +14 dB = x5
  loud = (np.ones(800) * 20000).astype(np.int16)
  out = np.frombuffer(Gain().apply(loud.tobytes()), dtype=np.int16)
  assert out.max() == 32767 and len(out) == 800   # clipped, never wrapped
  assert Gain().apply(b"") == b""
