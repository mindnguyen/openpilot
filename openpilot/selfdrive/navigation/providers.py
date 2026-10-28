"""Route providers for navd.

navd speaks the Mapbox Directions v5 dialect.  Three backends produce it:

  * comma   – comma's proxy (API_HOST/v1/nav, device JWT).  Historical default.
  * mapbox  – api.mapbox.com with a user token.
  * vietmap – VietMap Navigation endpoint, which is Mapbox-v5 compatible
              (https://maps.vietmap.vn/api/navigations/route/directions/v5/mapbox/<profile>/<coords>?apikey=...).

The backend is chosen by a small JSON file so no params/rebuild is needed:

  /data/navd_provider.json
  {"provider": "vietmap", "apikey": "<key>"}                      # VietMap
  {"provider": "mapbox",  "token": "pk...."}                       # Mapbox direct
  {"provider": "vietmap", "apikey": "...", "host": "...", "profile": "driving-traffic",
   "append_json": false, "language": "vi", "extra_params": {}}    # all knobs

When the file is missing navd behaves exactly as before.
"""
import json
import os

CONFIG_PATH = os.environ.get("NAVD_PROVIDER_CONFIG", "/data/navd_provider.json")

VIETMAP_HOST = "https://maps.vietmap.vn/api/navigations/route"
MAPBOX_HOST = "https://api.mapbox.com"

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
  if p in ("vietmap", "mapbox", "comma"):
    return p
  if cfg.get("apikey"):
    return "vietmap"
  if cfg.get("token") or mapbox_env_token:
    return "mapbox"
  return "comma"


def build_request(cfg: dict, provider: str, origin, destination, bearing, lang, comma_token=None,
                  mapbox_env_token=None, comma_host=None) -> tuple[str, dict]:
  """Return (url, query params) for a Directions v5 request origin -> destination ((lon, lat) tuples)."""
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
