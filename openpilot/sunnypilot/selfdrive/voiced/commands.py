"""
Vietnamese voice commands for the ICBM set speed: "Hi Comma, giảm về 50".

Pure Python on purpose (no openpilot imports) so it can be unit tested anywhere.

Supported after the wake phrase ("hi comma", heard by a Vietnamese model as "hai com ma"):
  giảm về / xuống / còn N     -> set N, must be lower than now
  tăng lên / tới / đến N      -> set N, must be higher than now
  đặt / chỉnh / cài N         -> set N
  N                           -> set N
  giảm / bớt / hạ N           -> N lower than now (N <= MAX_DELTA)
  tăng / thêm N               -> N higher than now (N <= MAX_DELTA)
The wake phrase may also be said alone; the command then has to follow within ARM_TIMEOUT seconds.
Numbers can be digits ("50") or words ("năm mươi", "sáu mươi lăm", "một trăm hai", "trăm linh năm").
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ARM_TIMEOUT = 5.0  # s: "Hi Comma" alone, then the command
MAX_DELTA = 30     # relative changes above this are too likely a misheard "giảm về"
MIN_ABSOLUTE = 10  # absolute targets below this are a misparse

# wake phrases, compared without diacritics
WAKE_PHRASES = [
  ("hi", "comma"), ("hey", "comma"), ("hai", "comma"),
  ("hi", "com", "ma"), ("hai", "com", "ma"), ("hay", "com", "ma"), ("hai", "cam", "ma"), ("hai", "co", "ma"),
  ("comma", "oi"), ("com", "ma", "oi"),
]
WAKE_MAX_OFFSET = 2  # allow a couple of filler words ("ê", "nào") before the wake phrase

DIGITS = {
  "không": 0, "một": 1, "hai": 2, "ba": 3, "bốn": 4, "năm": 5, "sáu": 6, "bảy": 7, "bẩy": 7, "tám": 8, "chín": 9,
}
# special unit forms after "mươi"
UNITS_AFTER_TENS = {**DIGITS, "mốt": 1, "tư": 4, "lăm": 5, "nhăm": 5}
UNITS_AFTER_MUOI = {**DIGITS, "lăm": 5, "nhăm": 5}  # after "mười": "mười lăm" = 15

VERBS_DOWN = {"giảm", "bớt", "hạ"}
VERBS_UP = {"tăng", "thêm"}
VERBS_SET = {"đặt", "chỉnh", "cài", "set", "để"}
DIR_DOWN = {"về", "xuống", "còn"}
DIR_UP = {"lên", "tới", "đến"}

# words that can appear around the number and are ignored
FILLER = {"tốc", "độ", "ga", "xe", "nhé", "nha", "đi", "ạ", "giúp", "cho", "tôi", "mình", "ơi", "ê", "nào", "ki", "lô", "mét",
          "km", "kmh", "cây", "số", "trên", "giờ", "h", "kilomet", "kilômét"}


def strip_accents(s: str) -> str:
  s = unicodedata.normalize("NFD", s)
  s = "".join(c for c in s if unicodedata.category(c) != "Mn")
  return s.replace("đ", "d").replace("Đ", "D")


def tokenize(text: str) -> list[str]:
  text = unicodedata.normalize("NFC", text.lower())
  text = text.replace("km/h", " km ").replace("km/giờ", " km ")
  text = re.sub(r"(\d+)\s*(km|kmh|k)\b", r"\1 km", text)
  return re.findall(r"\d+|[^\W\d_]+", text)


def find_wake(tokens: list[str]) -> int | None:
  """Index of the first token after the wake phrase, or None when there is no wake phrase."""
  plain = [strip_accents(t) for t in tokens]
  for start in range(min(WAKE_MAX_OFFSET + 1, len(plain))):
    for phrase in WAKE_PHRASES:
      if tuple(plain[start:start + len(phrase)]) == phrase:
        return start + len(phrase)
  return None


def parse_number(tokens: list[str], i: int) -> tuple[int, int] | None:
  """Parse a Vietnamese number (or digits) starting at tokens[i]. Returns (value, next index) or None."""
  n = len(tokens)
  if i >= n:
    return None
  if tokens[i].isdigit():
    return int(tokens[i]), i + 1

  total, j, hundreds = 0, i, False
  # hundreds: "một trăm", or bare "trăm"
  if j + 1 < n and tokens[j] in DIGITS and tokens[j + 1] == "trăm":
    total, j, hundreds = DIGITS[tokens[j]] * 100, j + 2, True
  elif tokens[j] == "trăm":
    total, j, hundreds = 100, j + 1, True

  if hundreds:
    if j < n and tokens[j] in ("linh", "lẻ"):
      if j + 1 < n and tokens[j + 1] in DIGITS:
        return total + DIGITS[tokens[j + 1]], j + 2
      return total, j + 1
    if j < n and tokens[j] == "rưỡi":
      return total + 50, j + 1
    # colloquial "một trăm hai" = 120 (a single digit right after "trăm" counts tens)
    if j < n and tokens[j] in DIGITS and not (j + 1 < n and tokens[j + 1] in ("mươi", "chục")):
      return total + DIGITS[tokens[j]] * 10, j + 1

  # tens
  if j < n and tokens[j] == "mười":
    total += 10
    j += 1
    if j < n and tokens[j] in UNITS_AFTER_MUOI:
      total += UNITS_AFTER_MUOI[tokens[j]]
      j += 1
    return total, j
  if j + 1 < n and tokens[j] in DIGITS and tokens[j + 1] in ("mươi", "chục"):
    total += DIGITS[tokens[j]] * 10
    j += 2
    if j < n and tokens[j] in UNITS_AFTER_TENS:
      total += UNITS_AFTER_TENS[tokens[j]]
      j += 1
    return total, j
  # a single digit
  if j < n and tokens[j] in DIGITS:
    return total + DIGITS[tokens[j]], j + 1

  return (total, j) if hundreds else None


@dataclass(frozen=True)
class VoiceCommand:
  kind: str              # "set" or "delta"
  value: int             # absolute speed for "set", signed change for "delta"
  direction: str | None  # "up"/"down" when the wording says which way a "set" must go
  text: str = ""


@dataclass(frozen=True)
class Rejected:
  reason: str
  text: str = ""


def parse_command(tokens: list[str], text: str = "") -> VoiceCommand | Rejected:
  """Parse the words after the wake phrase."""
  # fillers are skipped while looking for the verb, but kept after the number: "năm mươi ki lô mét một giờ"
  # must stop at "ki" and not read on into "một" (= 51)
  toks = list(tokens)
  verb = None
  direction = None
  i = 0
  while i < len(toks) and not toks[i].isdigit() and parse_number(toks, i) is None:
    t = toks[i]
    if t in VERBS_DOWN:
      verb = "down"
    elif t in VERBS_UP:
      verb = "up"
    elif t in VERBS_SET:
      verb = verb or "set"
    elif t in DIR_DOWN:
      direction = "down"
    elif t in DIR_UP:
      direction = "up"
    i += 1

  num = parse_number(toks, i)
  if num is None:
    return Rejected("no speed heard", text)
  value = num[0]

  if direction is not None or verb in (None, "set"):
    if verb in ("up", "down") and direction is not None and verb != direction:
      return Rejected("conflicting words", text)  # "giảm lên 50"
    if value < MIN_ABSOLUTE:
      return Rejected("speed too low", text)
    want = direction or (verb if verb in ("up", "down") else None)
    return VoiceCommand("set", value, want, text)

  # verb without direction: relative change
  if value <= 0 or value > MAX_DELTA:
    return Rejected("say 'giam ve' or 'tang len' for a target speed", text)
  return VoiceCommand("delta", value if verb == "up" else -value, None, text)


def resolve_target(cmd: VoiceCommand, current: int, v_min: int, v_max: int) -> tuple[int | None, str]:
  """Turn a command into a new set speed. Returns (target, "") or (None, reason)."""
  if cmd.kind == "delta":
    target = current + cmd.value
  else:
    target = cmd.value
    if cmd.direction == "down" and target >= current:
      return None, f"{target} is not lower than {current}"
    if cmd.direction == "up" and target <= current:
      return None, f"{target} is not higher than {current}"
  target = max(v_min, min(v_max, target))
  if target == current:
    return None, f"already {current}"
  return target, ""


class CommandSession:
  """Wake phrase handling across utterances: "Hi Comma" alone arms the next utterance for ARM_TIMEOUT."""

  def __init__(self):
    self.armed_until = -1.0

  def handle(self, text: str, now: float) -> VoiceCommand | Rejected | None:
    """None when the utterance is not for us (no wake phrase and not armed) or only armed the session."""
    tokens = tokenize(text)
    if not tokens:
      return None
    start = find_wake(tokens)
    if start is None:
      if now > self.armed_until:
        return None
      start = 0
    rest = tokens[start:]
    if not [t for t in rest if t not in FILLER]:
      self.armed_until = now + ARM_TIMEOUT
      return None
    self.armed_until = -1.0
    return parse_command(rest, text)


def grammar_words() -> list[str]:
  """Vocabulary for a grammar-restricted recognizer: keeps a small model accurate and cheap."""
  words = {w for phrase in WAKE_PHRASES for w in phrase}
  words |= {"hây", "cóm", "ơi"}
  words |= set(DIGITS) | set(UNITS_AFTER_TENS) | {"mười", "mươi", "chục", "trăm", "linh", "lẻ", "rưỡi"}
  words |= VERBS_DOWN | VERBS_UP | VERBS_SET | DIR_DOWN | DIR_UP
  words |= {"tốc", "độ", "ki", "lô", "mét", "cây", "số", "trên", "giờ", "nhé", "đi"}
  return sorted(words)
