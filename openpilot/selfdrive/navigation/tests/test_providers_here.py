from openpilot.selfdrive.navigation import providers as P


def test_flexible_polyline_readme_example():
  # https://github.com/heremaps/flexible-polyline README: precision 5, no third dimension
  pts = P.decode_flexible_polyline("BFoz5xJ67i1B1B7PzIhaxL7Y")
  assert [[round(lon, 5), round(lat, 5)] for lon, lat in pts] == [
    [8.69821, 50.10228], [8.69567, 50.10201], [8.69150, 50.10063], [8.68752, 50.09878]]


def test_provider_name_here():
  assert P.provider_name({"provider": "here", "here_apikey": "k"}, None) == "here"
  assert P.provider_name({"here_apikey": "k"}, None) == "here"
  assert P.provider_name({"provider": "vietmap", "apikey": "v", "here_apikey": "k"}, None) == "vietmap"


def test_build_here_request():
  url, q = P.build_request({"provider": "here", "here_apikey": "k", "traffic": False}, "here", (106.70, 10.77), (106.72, 10.80), None, "vi")
  assert url == P.HERE_HOST
  assert q["origin"] == "10.770000,106.700000" and q["destination"] == "10.800000,106.720000"
  assert q["apiKey"] == "k" and q["lang"] == "vi-VN" and q["traffic[mode]"] == "disabled"
  assert "polyline" in q["return"] and "maxSpeed" in q["spans"]


def _here_response():
  # straight line of 6 points ~100 m apart along a parallel; encode with precision 5 by hand is tedious, so build
  # the polyline through the decoder's inverse: here we only need decode(), so use a known-good encoded string
  # for two points and synthesise the rest with a monkeypatched decoder in the test.
  return {"routes": [{"sections": [{
    "polyline": "SYNTH",
    "actions": [
      {"action": "depart", "duration": 60, "length": 300, "offset": 0, "instruction": "Đi về hướng tây trên Nguyễn Huệ."},
      {"action": "turn", "direction": "left", "severity": "quite", "duration": 30, "length": 200, "offset": 3,
       "instruction": "Rẽ trái vào Lê Lợi."},
      {"action": "arrive", "duration": 0, "length": 0, "offset": 5, "instruction": "Đến nơi."},
    ],
    "turnByTurnActions": [
      {"action": "depart", "offset": 0, "currentRoad": {"name": [{"value": "Nguyễn Huệ", "language": "vi"}]}},
      {"action": "turn", "direction": "left", "offset": 3, "currentRoad": {"name": [{"value": "Nguyễn Huệ", "language": "vi"}]},
       "nextRoad": {"name": [{"value": "Lê Lợi", "language": "vi"}]}},
      {"action": "arrive", "offset": 5},
    ],
    "spans": [{"offset": 0, "maxSpeed": 13.89}, {"offset": 2, "maxSpeed": "unlimited"}, {"offset": 3, "maxSpeed": 16.67}],
  }]}]}


def test_here_route_steps(monkeypatch):
  pts = [[106.700 + 0.001 * i, 10.770] for i in range(6)]  # ~109 m apart
  monkeypatch.setattr(P, "decode_flexible_polyline", lambda s: pts)
  steps = P.here_route_steps(_here_response())
  assert len(steps) == 3
  s0, s1, s2 = steps
  assert s0["geometry"]["coordinates"] == pts[0:4] and s1["geometry"]["coordinates"] == pts[3:6]
  assert s0["name"] == "Nguyễn Huệ" and s0["maneuver"]["type"] == "depart"
  # banner of step 0 describes the next maneuver: left turn onto Lê Lợi
  b = s0["bannerInstructions"][0]
  assert b["primary"] == {"text": "Lê Lợi", "type": "turn", "modifier": "left"} and b["distanceAlongGeometry"] == 300
  assert b["secondary"]["text"] == "Rẽ trái vào Lê Lợi."
  assert s2["maneuver"]["type"] == "arrive" and s2["bannerInstructions"] == []
  # speed limits: 50 km/h for segments 0-1, unknown for segment 2, 60 km/h from segment 3
  assert [round(l * 3.6) for _, l in s0["speed_limits"]] == [50, 0]
  assert [round(l * 3.6) for _, l in s1["speed_limits"]] == [60]
  assert round(P.limit_at(s0, 50.) * 3.6) == 50
  assert P.limit_at(s0, 300.) == 0.
  assert round(P.limit_at(s1, 10.) * 3.6) == 60
  assert P.limit_at({"speed_limits": []}, 5.) == 0.
  # route_steps dispatch
  assert P.route_steps("here", {"routes": []}) == []
  assert len(P.route_steps("here", _here_response())) == 3


def test_here_maneuver_mapping():
  m = P.here_maneuver
  assert m({"action": "turn", "direction": "right", "severity": "light"}) == ("turn", "slight right")
  assert m({"action": "turn", "direction": "left", "severity": "heavy"}) == ("turn", "sharp left")
  assert m({"action": "uTurn", "direction": "left"}) == ("turn", "uturn")
  assert m({"action": "keep", "direction": "middle"}) == ("fork", "straight")
  assert m({"action": "exit", "direction": "right"}) == ("off ramp", "slight right")
  assert m({"action": "roundaboutEnter", "direction": "right"}) == ("roundabout", "right")
  assert m({"action": "continueHighway"}) == ("continue", "straight")
