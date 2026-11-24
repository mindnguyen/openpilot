import sys
import time
from pathlib import Path

from openpilot.common.hardware import AGNOS
from openpilot.common.hardware.usb import CHESTNUT_USB_PRODUCT, USB_DEVICES_PATH, is_chestnut_usb_id

MODELS_DIR = Path(__file__).resolve().parent / 'models'


def modeld_pkl_path(chestnut: bool):
  prefix = 'big_' if chestnut else ''
  return MODELS_DIR / f'{prefix}driving_tinygrad.pkl'

def load_oob(path, chestnut=False):
  from tinygrad import Context
  device = 'USB+AMD:LLVM' if chestnut else 'QCOM' if AGNOS else 'METAL' if sys.platform == 'darwin' else 'CPU:LLVM'
  with Context(DEV=device):
    from tinygrad_repo.examples.openpilot.helpers import load_pickle
    return load_pickle(path, out_of_band=True)

def chestnut_present() -> bool:
  for d in USB_DEVICES_PATH.glob("*"):
    try:
      usb_id = (int((d / "idVendor").read_text(), 16), int((d / "idProduct").read_text(), 16))
      product = (d / "product").read_text().strip()
      if is_chestnut_usb_id(*usb_id) and product == CHESTNUT_USB_PRODUCT:
        return True
    except Exception:
      pass
  return False

def wait_for_chestnut(timeout: float = 10.) -> None:
  # the cable is detected before chestnut enumerates
  st = time.monotonic()
  while not chestnut_present():
    if time.monotonic() - st > timeout:
      raise TimeoutError("chestnut did not enumerate")
    time.sleep(0.1)

BIG_MODEL_RETRY_DELAY = 1.  # s after the failure: the engine runs by then, and the new modeld touches the chestnut only ~10 s later


class BigModelRetry:
  """Bring the big model back after it died while running.

  The usual cause is the starter: cranking the engine after the model has loaded pulls the 12V down, the chestnut
  drops off for a moment and the model falls back to small until the next ignition. A failed load is already retried
  once with an onroad cycle (ChestnutRetry); this does the same for a failure after the load, but only while the cycle
  cannot disturb driving: selector in P or N, car standing, openpilot not engaged, chestnut on USB. That is the state of
  a normal car start, so the cycle is no more than a second start-up. Anything else (an engine restart at a traffic
  light, say) keeps the small model and waits until the car is parked."""

  def __init__(self, params, enabled: bool = True):
    self.params = params
    self.enabled = enabled
    self.failed_t: float | None = None

  @property
  def pending(self) -> bool:
    return self.failed_t is not None

  def failed(self, now: float) -> None:
    # one cycle per ignition, shared with the retry of a failed load
    if self.enabled and not self.params.get_bool("ChestnutRetry"):
      self.failed_t = now

  def update(self, now: float, parked: bool, v_ego: float, engaged: bool, chestnut_usb: bool) -> bool:
    """True when the onroad cycle was requested. Call once per frame while pending."""
    if self.failed_t is None or now - self.failed_t < BIG_MODEL_RETRY_DELAY:
      return False
    if not parked or v_ego > 0.1 or engaged or not chestnut_usb:
      return False
    self.failed_t = None
    if self.params.get_bool("ChestnutRetry"):   # the load retry took the one cycle in the meantime
      return False
    self.params.put_bool("ChestnutRetry", True)
    self.params.put_bool("OnroadCycleRequested", True)
    return True


def chestnut_compiled() -> bool:
  path = modeld_pkl_path(chestnut=True)
  return path.is_file() and all(
    (MODELS_DIR / f'big_driving_warp_{size}_tinygrad.pkl').is_file() for size in ('1344x760', '1928x1208'))
