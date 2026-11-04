"""
External speed limit source for the sunnypilot speed limit resolver.

Traffic signs read on the Android HMI (the VietMap Live widget) are pushed to commaview, which writes them as a
small JSON record to EXT_LIMIT_PATH (/dev/shm, so nothing hits the flash):

  {"limit_kph": 80, "next_kph": 60, "next_dist_m": 200, "camera_dist_m": 400, "source": "vietmap_live", "ts": <unix>}

The record is only trusted while it is fresh (EXT_LIMIT_MAX_AGE); the HMI has to repeat it every few seconds and
send limit_kph 0 when the sign disappears.  Pure stdlib so commaview and the unit test can import it on any machine.
"""
import json
import os
import time

EXT_LIMIT_PATH = "/dev/shm/params/ExternalSpeedLimit"
EXT_LIMIT_MAX_AGE = 8.0  # s without a fresh record -> source drops out
KPH_TO_MS = 1.0 / 3.6


class ExternalLimit:
  def __init__(self, path: str = EXT_LIMIT_PATH):
    self.path = path
    self._mtime = None
    self._rec = None

  def read(self, now: float | None = None, v_ego: float = 0.0):
    """Returns (limit_ms, next_limit_ms, next_distance_m) or None when there is no fresh sign.

    The file is re-parsed only when its mtime changes, so calling this at planner rate is cheap.  The distance to the
    next sign is reduced by the distance driven since the record was written."""
    now = time.time() if now is None else now
    try:
      st = os.stat(self.path)
    except OSError:
      self._rec = None
      self._mtime = None
      return None
    if st.st_mtime != self._mtime:
      self._mtime = st.st_mtime
      try:
        with open(self.path) as f:
          rec = json.load(f)
        self._rec = rec if isinstance(rec, dict) else None
      except (OSError, ValueError):
        self._rec = None
    rec = self._rec
    if not rec:
      return None
    try:
      age = now - float(rec.get("ts", 0.0))
      limit = float(rec.get("limit_kph", 0)) * KPH_TO_MS
      nxt = float(rec.get("next_kph", 0)) * KPH_TO_MS
      dist = float(rec.get("next_dist_m", 0))
    except (TypeError, ValueError):
      return None
    if age > EXT_LIMIT_MAX_AGE or limit <= 0.0:
      return None
    if nxt <= 0.0:
      return limit, 0.0, 0.0
    dist = max(0.0, dist - max(0.0, v_ego) * max(0.0, age))
    return limit, nxt, dist
