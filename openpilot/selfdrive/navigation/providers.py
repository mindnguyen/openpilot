"""Route providers for navd.

navd speaks the Mapbox Directions v5 dialect.  Three backends produce it:

  * comma   – comma's proxy (API_HOST/v1/nav, device JWT).  Historical default.
  * mapbox  – api.mapbox.com with a user token.
  * vietmap – VietMap Navigation endpoint, which is Mapbox-v5 compatible
              (https://maps.vietmap.vn/api/navigations/route/directions/v5/mapbox/<profile>/<coords>?apikey=...).
  * here    – HERE Routing v8 (https://router.hereapi.com/v8/routes).  Not Mapbox-shaped: here_route_steps()
              turns sections/actions/spans into the same step dialect, and keeps the per-segment speed limit
              (spans=maxSpeed) in step["speed_limits"] so navd can publish navInstruction.speedLimit.

The backend is chosen by a small JSON file so no params/rebuild is needed:

  /data/navd_provider.json
  {"provider": "vietmap", "apikey": "<key>"}                      # VietMap
  {"provider": "mapbox",  "token": "pk...."}                       # Mapbox direct
  {"provider": "here",    "here_apikey": "<key>", "traffic": true}  # HERE (traffic-aware routing on by default)
  {"provider": "vietmap", "apikey": "...", "host": "...", "profile": "driving-traffic",
   "append_json": false, "language": "vi", "extra_params": {}}    # all knobs

When the file is missing navd behaves exactly as before.
"""
import json
import os

CONFIG_PATH = os.environ.get("NAVD_PROVIDER_CONFIG", "/data/navd_provider.json")

VIETMAP_HOST = "https://maps.vietmap.vn/api/navigations/route"
MAPBOX_HOST = "https://api.mapbox.com"
HERE_HOST = "https://router.hereapi.com/v8/routes"

DIRECTION_MAP = {  # Mapbox lane direction -> cereal NavInstruction.Direction
  "left": "left", "right": "right", "straight": "straight",
  "slight left": "slightLeft", "slight right": "slightRight",
  "sharp left": "left", "sharp right": "right", "uturn": "none",
}


def load_config(path: str = CONFIG_PATH) -> dict:
  try:
    with open(path) as f:
      cfg = json.load(f)
    return cfg if isinstance(cfg, dict) else {}
  except (OSError, ValueError):
    return {}


def provider_name(cfg: dict, mapbox_env_token: str | None) -> str:
  p = str(cfg.get("provider", "")).lower()
  if p in ("vietmap", "mapbox", "comma", "here"):
    return p
  if cfg.get("here_apikey"):
    return "here"
  if cfg.get("apikey"):
    return "vietmap"
  if cfg.get("token") or mapbox_env_token:
    return "mapbox"
  return "comma"


def build_request(cfg: dict, provider: str, origin, destination, bearing, lang, comma_token=None,
                  mapbox_env_token=None, comma_host=None) -> tuple[str, dict]:
  """Return (url, query params) for a route request origin -> destination ((lon, lat) tuples)."""
  if provider == "here":
    return build_here_request(cfg, origin, destination, lang)
  profile = cfg.get("profile", "driving-traffic")
  coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in (origin, destination))
  params = {
    "geometries": "geojson",
    "overview": "full",
    "steps": "true",
    "banner_instructions": "true",
    "voice_instructions": "false",
    "alternatives": "false",
  }
  language = cfg.get("language") or lang
  if language:
    params["language"] = language
  if bearing is not None:
    params["bearings"] = f"{(bearing + 360) % 360:.0f},90;"

  if provider == "vietmap":
    host = cfg.get("host", VIETMAP_HOST).rstrip("/")
    url = f"{host}/directions/v5/mapbox/{profile}/{coords}"
    if cfg.get("append_json"):  # the iOS SDK appends .json; Android does not
      url += ".json"
    params["apikey"] = cfg.get("apikey", "")
  elif provider == "mapbox":
    host = cfg.get("host", MAPBOX_HOST).rstrip("/")
    url = f"{host}/directions/v5/mapbox/{profile}/{coords}"
    params["access_token"] = cfg.get("token") or mapbox_env_token or ""
  else:  # comma proxy
    url = f"{comma_host}/directions/v5/mapbox/{profile}/{coords}"
    params["access_token"] = comma_token or ""
  params.update(cfg.get("extra_params", {}) or {})
  return url, params


def decode_polyline(encoded: str, precision: int = 6) -> list[list[float]]:
  """Google encoded polyline -> [[lon, lat], ...]."""
  coords, index, lat, lon = [], 0, 0, 0
  factor = 10 ** precision
  while index < len(encoded):
    for which in (0, 1):
      result, shift = 0, 0
      while True:
        b = ord(encoded[index]) - 63
        index += 1
        result |= (b & 0x1f) << shift
        shift += 5
        if b < 0x20:
          break
      delta = ~(result >> 1) if result & 1 else result >> 1
      if which == 0:
        lat += delta
      else:
        lon += delta
    coords.append([lon / factor, lat / factor])
  return coords


def _plausible(coords: list[list[float]], near) -> bool:
  if not coords:
    return False
  lon, lat = coords[0]
  if not (-180 <= lon <= 180 and -90 <= lat <= 90):
    return False
  return near is None or (abs(lon - near[0]) < 2.0 and abs(lat - near[1]) < 2.0)


def normalize_steps(steps: list[dict], near=None) -> list[dict]:
  """Make every step carry step['geometry']['coordinates'] as [[lon, lat], ...] and a bannerInstructions list.

  Providers that ignore geometries=geojson return polyline strings; decode those (polyline6, then polyline5).
  """
  for step in steps:
    geom = step.get("geometry")
    if isinstance(geom, str):
      coords = decode_polyline(geom, 6)
      if not _plausible(coords, near):
        coords = decode_polyline(geom, 5)
      step["geometry"] = {"type": "LineString", "coordinates": coords}
    elif isinstance(geom, dict) and "coordinates" not in geom:
      step["geometry"] = {"type": "LineString", "coordinates": []}
    elif geom is None:
      step["geometry"] = {"type": "LineString", "coordinates": []}
    step.setdefault("bannerInstructions", [])
    step.setdefault("distance", 0.0)
    step.setdefault("duration", 0.0)
    for b in step["bannerInstructions"]:
      b.setdefault("distanceAlongGeometry", step["distance"])
      b.setdefault("primary", {})
  return steps


def banner_lanes(banner: dict) -> list[dict]:
  """Lane components of a banner -> [{'directions': [...cereal enum names], 'active': bool, 'activeDirection': name}]."""
  lanes = []
  comps = (banner.get("sub") or {}).get("components") or []
  if not any(c.get("type") == "lane" for c in comps):
    comps = (banner.get("primary") or {}).get("components") or []
  for c in comps:
    if c.get("type") != "lane":
      continue
    dirs = [DIRECTION_MAP.get(str(d).lower(), "none") for d in (c.get("directions") or [])]
    lanes.append({
      "directions": dirs or ["none"],
      "active": bool(c.get("active", False)),
      "activeDirection": DIRECTION_MAP.get(str(c.get("active_direction", "")).lower(), "none"),
    })
  return lanes


def route_speed_limits(leg: dict) -> list[float]:
  """annotation.maxspeed -> list of m/s per segment (0 when unknown)."""
  out = []
  for m in (leg.get("annotation") or {}).get("maxspeed") or []:
    if not isinstance(m, dict) or m.get("unknown") or m.get("none"):
      out.append(0.0)
      continue
    speed = float(m.get("speed", 0) or 0)
    out.append(speed / 3.6 if m.get("unit", "km/h") == "km/h" else speed * 0.44704)
  return out


# ---------------------------------------------------------------------------------------------------------------
# HERE Routing v8
# ---------------------------------------------------------------------------------------------------------------

def here_key(cfg: dict) -> str:
  return str(cfg.get("here_apikey") or (cfg.get("apikey") if str(cfg.get("provider", "")).lower() == "here" else "") or "")


def build_here_request(cfg: dict, origin, destination, lang) -> tuple[str, dict]:
  (olon, olat), (dlon, dlat) = origin, destination
  language = cfg.get("language") or "vi-VN"
  if language and "-" not in language:
    language = {"vi": "vi-VN", "en": "en-US"}.get(language, language)
  params = {
    "transportMode": "car",
    "origin": f"{olat:.6f},{olon:.6f}",
    "destination": f"{dlat:.6f},{dlon:.6f}",
    "return": "polyline,actions,instructions,turnByTurnActions,summary",
    "spans": "maxSpeed,names,length",
    "lang": language,
    "units": "metric",
    "apiKey": here_key(cfg),
  }
  if not cfg.get("traffic", True):
    params["traffic[mode]"] = "disabled"  # cheaper price bucket, no live traffic in the ETA
  params.update(cfg.get("extra_params", {}) or {})
  return cfg.get("host", HERE_HOST), params


_FP_TABLE = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")}


def decode_flexible_polyline(encoded: str) -> list[list[float]]:
  """HERE flexible polyline -> [[lon, lat], ...] (a third dimension, if present, is dropped)."""
  values = []
  result = shift = 0
  for c in encoded:
    v = _FP_TABLE[c]
    result |= (v & 0x1F) << shift
    if v & 0x20:
      shift += 5
    else:
      values.append(result)
      result = shift = 0
  if len(values) < 2:
    return []
  header = values[1]
  precision = header & 15
  third_dim = (header >> 4) & 7
  factor = 10 ** precision
  n = 3 if third_dim else 2
  lat = lon = 0
  out = []
  for i in range(2, len(values) - n + 1, n):
    dlat, dlon = values[i], values[i + 1]
    lat += ~(dlat >> 1) if dlat & 1 else dlat >> 1
    lon += ~(dlon >> 1) if dlon & 1 else dlon >> 1
    out.append([lon / factor, lat / factor])
  return out


def _haversine_m(a, b) -> float:
  import math
  lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
  h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
  return 2 * 6371000.0 * math.asin(math.sqrt(h))


def here_maneuver(a: dict) -> tuple[str, str]:
  """HERE action -> (Mapbox maneuver type, modifier) so the comma UI picks the right icon."""
  act, d, sev = a.get("action", ""), a.get("direction", ""), a.get("severity", "")
  side = {"left": "left", "right": "right"}.get(d, "straight")
  if act == "depart":
    return "depart", side if d in ("left", "right") else ""
  if act == "arrive":
    return "arrive", side if d in ("left", "right") else ""
  if act == "turn":
    if sev == "light":
      return "turn", f"slight {side}" if side != "straight" else "straight"
    if sev == "heavy":
      return "turn", f"sharp {side}" if side != "straight" else "straight"
    return "turn", side
  if act == "uTurn":
    return "turn", "uturn"
  if act == "keep":
    return "fork", f"slight {side}" if side != "straight" else "straight"
  if act == "ramp":
    return "on ramp", f"slight {side}" if side != "straight" else "straight"
  if act == "exit":
    return "off ramp", f"slight {side}" if side != "straight" else "straight"
  if act == "enterHighway":
    return "merge", f"slight {side}" if side != "straight" else "straight"
  if act == "roundaboutEnter":
    return "roundabout", side
  if act == "roundaboutExit":
    return "exit roundabout", side
  return "continue", "straight"  # continue, continueHighway, roundaboutPass, anything new


def _road_name(info) -> str:
  for n in (info or {}).get("name") or []:
    if n.get("value"):
      return str(n["value"])
  for n in (info or {}).get("number") or []:
    if n.get("value"):
      return str(n["value"])
  return ""


def _span_limits(spans: list[dict], n_points: int) -> list[float]:
  """maxSpeed per polyline segment k (between coords k and k+1), 0 when unknown/unlimited."""
  limits = [0.0] * max(n_points - 1, 0)
  current, next_i = 0.0, 0
  spans = sorted((sp for sp in spans or [] if isinstance(sp.get("offset"), int)), key=lambda sp: sp["offset"])
  for k in range(len(limits)):
    while next_i < len(spans) and spans[next_i]["offset"] <= k:
      ms = spans[next_i].get("maxSpeed")
      current = float(ms) if isinstance(ms, (int, float)) and ms > 0 else 0.0
      next_i += 1
    limits[k] = current
  return limits


def here_route_steps(r: dict) -> list[dict]:
  """HERE Routing v8 response -> Mapbox-dialect steps (geometry, distance, duration, maneuver, bannerInstructions)
  plus step["speed_limits"] = [(metres from step start where this limit ends, m/s), ...]."""
  steps: list[dict] = []
  for sec in (r.get("routes") or [{}])[0].get("sections") or []:
    coords = decode_flexible_polyline(sec.get("polyline") or "")
    if len(coords) < 2:
      continue
    tbt = sec.get("turnByTurnActions") or []
    acts = sec.get("actions") or tbt
    if not acts:
      continue
    roads = {a.get("offset"): a for a in tbt}
    limits = _span_limits(sec.get("spans") or [], len(coords))
    entries = []
    for a in acts:
      o = a.get("offset", 0)
      o = max(0, min(int(o), len(coords) - 1))
      entries.append((o, a, roads.get(o, a)))
    for i, (o, a, road) in enumerate(entries):
      nxt = entries[i + 1][0] if i + 1 < len(entries) else len(coords) - 1
      geom = coords[o:max(nxt, o) + 1]
      if len(geom) < 2:
        geom = [coords[o], coords[min(o + 1, len(coords) - 1)]]
      mtype, mmod = here_maneuver(a)
      step = {
        "distance": float(a.get("length", 0) or 0),
        "duration": float(a.get("duration", 0) or 0),
        "name": _road_name(road.get("currentRoad")) or _road_name(road.get("nextRoad")),
        "geometry": {"type": "LineString", "coordinates": geom},
        "maneuver": {"type": mtype, "modifier": mmod, "instruction": a.get("instruction", ""), "location": coords[o]},
        "bannerInstructions": [],
      }
      if i + 1 < len(entries):
        _, na, nroad = entries[i + 1]
        ntype, nmod = here_maneuver(na)
        # banner text: the road to turn onto; else the first sentence of the instruction ("Rẽ phải sang đường X.")
        text = _road_name(nroad.get("nextRoad")) or (na.get("instruction", "") or "").split(". ")[0].rstrip(".")
        banner = {"distanceAlongGeometry": step["distance"], "primary": {"text": text, "type": ntype, "modifier": nmod}}
        if na.get("instruction") and text != na.get("instruction"):
          banner["secondary"] = {"text": na["instruction"]}
        step["bannerInstructions"] = [banner]
      # speed limits along this step: cumulative end distance per constant-limit run
      runs, cum = [], 0.0
      for k in range(o, max(nxt, o)):
        cum += _haversine_m(coords[k], coords[k + 1])
        lim = limits[k] if k < len(limits) else 0.0
        if runs and runs[-1][1] == lim:
          runs[-1] = (cum, lim)
        else:
          runs.append((cum, lim))
      step["speed_limits"] = runs
      steps.append(step)
  return steps


def route_steps(provider: str, r: dict, near=None) -> list[dict]:
  """Steps for navd from any provider response (empty list when the response has no route)."""
  if provider == "here":
    return here_route_steps(r)
  routes = r.get("routes") or []
  if not routes:
    return []
  return normalize_steps(routes[0]["legs"][0]["steps"], near=near)


def limit_at(step: dict, along_m: float) -> float:
  """Speed limit (m/s) at along_m metres into the step, 0 when unknown."""
  runs = step.get("speed_limits") or []
  for end, lim in runs:
    if along_m <= end:
      return float(lim)
  return float(runs[-1][1]) if runs else 0.0
