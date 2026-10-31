"""Audio conditioning for voiced: numpy only, so it can be tested and replayed on a laptop."""
import numpy as np


class Gain:
  """Lift quiet cabin speech towards the level the model expects.

  The comma microphone sits at the windshield, far from the driver: on the 2026-10-31 cabin recording speech peaked
  at -20..-28 dBFS and the small Vosk model heard "Hi Comma" as "khai mạc".  A fixed +14 dB on the same recording
  gave "hai com giảm về năm lăm" and three parsed commands; an adaptive gain (peak tracker, even slow) modulated the
  words and did worse, so the gain is a constant and only clipping is guarded against.
  """
  GAIN_DB = 14.0

  def __init__(self, gain_db: float = GAIN_DB):
    self.gain = 10 ** (gain_db / 20.)

  def apply(self, data: bytes) -> bytes:
    x = np.frombuffer(data[:len(data) // 2 * 2], dtype=np.int16).astype(np.float32) * self.gain
    return np.clip(x, -32768., 32767.).astype(np.int16).tobytes()
