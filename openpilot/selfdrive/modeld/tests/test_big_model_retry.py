from openpilot.selfdrive.modeld.helpers import BIG_MODEL_RETRY_DELAY, BigModelRetry


class FakeParams:
  def __init__(self, **kv):
    self.d = dict(kv)

  def get_bool(self, k):
    return bool(self.d.get(k, False))

  def put_bool(self, k, v):
    self.d[k] = bool(v)


PARKED = dict(parked=True, v_ego=0., engaged=False, chestnut_usb=True)
T = BIG_MODEL_RETRY_DELAY + 0.1


def cycled(p):
  return p.d.get("OnroadCycleRequested", False)


class TestBigModelRetry:
  def test_nothing_without_a_failure(self):
    p = FakeParams()
    r = BigModelRetry(p)
    assert not r.pending
    assert not r.update(100., **PARKED)
    assert not cycled(p)

  def test_cycles_once_when_parked(self):
    p = FakeParams()
    r = BigModelRetry(p)
    r.failed(10.)
    assert r.pending
    assert not r.update(10. + BIG_MODEL_RETRY_DELAY - 0.1, **PARKED)   # the crank may still be going on
    assert not cycled(p)
    assert r.update(10. + T, **PARKED)
    assert cycled(p) and p.get_bool("ChestnutRetry")
    assert not r.pending
    p.put_bool("OnroadCycleRequested", False)   # hardwared takes the request
    assert not r.update(20., **PARKED)
    assert not cycled(p)

  def test_never_while_driving_or_engaged(self):
    for bad in (dict(parked=False), dict(v_ego=0.5), dict(engaged=True), dict(chestnut_usb=False)):
      p = FakeParams()
      r = BigModelRetry(p)
      r.failed(0.)
      for t in (T, 10., 600.):
        assert not r.update(t, **{**PARKED, **bad}), bad
      assert not cycled(p), bad
      assert r.pending   # still waiting for the car to be parked

  def test_waits_for_park_then_cycles(self):
    p = FakeParams()
    r = BigModelRetry(p)
    r.failed(0.)   # engine restarted at a traffic light, in D
    assert not r.update(5., **{**PARKED, "parked": False})
    assert r.update(300., **PARKED)
    assert cycled(p)

  def test_one_cycle_per_ignition(self):
    p = FakeParams(ChestnutRetry=True)   # the retry of a failed load was already used
    r = BigModelRetry(p)
    r.failed(0.)
    assert not r.pending
    assert not r.update(T, **PARKED)
    assert not cycled(p)

  def test_load_retry_in_between_takes_the_cycle(self):
    p = FakeParams()
    r = BigModelRetry(p)
    r.failed(0.)
    p.put_bool("ChestnutRetry", True)
    assert not r.update(T, **PARKED)
    assert not cycled(p) and not r.pending

  def test_disabled_for_demo_or_no_chestnut(self):
    p = FakeParams()
    r = BigModelRetry(p, enabled=False)
    r.failed(0.)
    assert not r.pending
    assert not r.update(T, **PARKED)
    assert not cycled(p)
