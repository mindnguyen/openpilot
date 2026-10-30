import pytest

from openpilot.sunnypilot.selfdrive.voiced.commands import (
  CommandSession, Rejected, VoiceCommand, grammar_words, parse_number, resolve_target, tokenize,
)


@pytest.mark.parametrize("words, value", [
  ("50", 50), ("năm mươi", 50), ("tám mươi", 80), ("sáu mươi lăm", 65), ("năm mươi năm", 55),
  ("hai mươi mốt", 21), ("hai mươi tư", 24), ("mười", 10), ("mười lăm", 15), ("năm chục", 50),
  ("một trăm", 100), ("một trăm hai mươi", 120), ("một trăm hai", 120), ("trăm linh năm", 105),
  ("một trăm lẻ năm", 105), ("trăm rưỡi", 150), ("bảy", 7),
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
])
def test_commands(text, expected):
  got = cmd(text)
  assert isinstance(got, VoiceCommand)
  assert (got.kind, got.value, got.direction) == (expected.kind, expected.value, expected.direction)


@pytest.mark.parametrize("text", [
  "hai com ma giảm",                # no number
  "hai com ma giảm năm mươi",       # 50 lower is ambiguous, ask for "giảm về"
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
