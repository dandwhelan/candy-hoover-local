"""
Local (LAN) protocol for Candy / Hoover "simply-Fi" appliances.

No cloud, no account: the appliance runs a tiny HTTP server on port 80.

  read   GET http://<ip>/http-read.json?encrypted=0          -> plaintext JSON
         GET http://<ip>/http-read.json?encrypted=1          -> hex ciphertext
  write  GET http://<ip>/http-write.json?encrypted=0&<params>
         GET http://<ip>/http-write.json?encrypted=1&data=<hex>

Cipher:
  repeating-key XOR over the raw bytes, hex-encoded uppercase. Symmetric.
"""

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 80
TIMEOUT = 4.0

# Root key of the status document -> appliance family.
STATUS_ROOTS = {
    "statusLavatrice": "washer",
    "statusTD": "tumbledryer",
    "statusDWash": "dishwasher",
    "statusForno": "oven",
    "statusHob": "hob",
    "statusRX": "fridge",
}

HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


# --------------------------------------------------------------------------
# cipher
# --------------------------------------------------------------------------

def xor_bytes(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def encrypt(plaintext: str, key: str) -> str:
    """Repeating-key XOR -> uppercase hex."""
    return xor_bytes(plaintext.encode("utf-8"), key.encode("utf-8")).hex().upper()


def decrypt(hex_text: str, key: str) -> str:
    """Inverse of xor_encrypt (the cipher is symmetric)."""
    raw = bytes.fromhex(hex_text.strip())
    return xor_bytes(raw, key.encode("utf-8")).decode("utf-8", "replace")


KEY_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_MAX_COMBOS = 200000


def _key_prefixes():
    """
    Known-plaintext prefixes for key recovery.

    Real firmware pretty-prints its JSON with CRLF and tabs
    ({\\r\\n\\t"statusLavatrice":{\\r\\n\\t\\t"...), so assuming compact JSON
    silently breaks recovery on actual hardware. Cover the whitespace variants.
    """
    gaps = ("", "\r\n\t", "\n\t", "\r\n  ", "\n  ", " ", "\r\n\t\t", "\n\t\t")
    out = []
    for root in STATUS_ROOTS:
        for after_brace in gaps:
            for after_colon in ("", " "):
                for after_inner in gaps:
                    out.append(("{" + after_brace + '"' + root + '":' +
                                after_colon + "{" + after_inner + '"').encode())
    # longest first: more known plaintext means fewer unknown key bytes
    return sorted(set(out), key=len, reverse=True)


def recover_key(hex_text: str, key_lengths=(16, 8, 32)):
    """
    Recover the appliance key from one encrypted reply, without touching the phone.

    The cipher is a repeating-key XOR and every status document opens with a known
    root key, so known plaintext gives us the leading key bytes outright. Where the
    root is shorter than the key (statusTD, statusHob) the remaining bytes are
    pinned by the rest of the ciphertext: the key is alphanumeric, and every byte
    it covers must decrypt to printable ASCII, which almost always leaves one
    candidate per position.
    """
    import itertools

    try:
        ct = bytes.fromhex(hex_text.strip())
    except ValueError:
        return None

    for klen in key_lengths:
        if len(ct) < klen:
            continue
        for prefix in _key_prefixes():
            known = min(len(prefix), klen)
            key = [ct[i] ^ prefix[i] for i in range(known)]
            if not all(chr(c) in KEY_ALPHABET for c in key):
                continue

            # Narrow each still-unknown position using the rest of the ciphertext.
            options, combos = [], 1
            for i in range(known, klen):
                positions = range(i, len(ct), klen)
                ok = [ord(c) for c in KEY_ALPHABET
                      if all(32 <= (ct[j] ^ ord(c)) < 127 for j in positions)]
                if not ok:
                    options = None
                    break
                options.append(ok)
                combos *= len(ok)
            if options is None or combos > _MAX_COMBOS:
                continue

            for tail in itertools.product(*options) if options else [()]:
                cand = bytes(key + list(tail))
                try:
                    text = xor_bytes(ct, cand).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if not text.startswith(prefix.decode()):
                    continue
                try:
                    json.loads(re.sub(r",\s*([}\]])", r"\1", text))
                except ValueError:
                    continue
                return cand.decode()
    return None


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

_locks = {}
_locks_guard = threading.Lock()


def _lock_for(url: str):
    """
    One lock per appliance.

    These are tiny embedded HTTP servers: two overlapping requests and one of
    them simply times out. Serialising per host keeps a LAN sweep parallel
    across different appliances while never doubling up on any single one.
    """
    host = urllib.parse.urlparse(url).netloc or url
    with _locks_guard:
        return _locks.setdefault(host, threading.Lock())


def _get(url: str, timeout: float = TIMEOUT, attempts: int = 2) -> str:
    last = None
    with _lock_for(url):
        for n in range(attempts):
            req = urllib.request.Request(url, headers={"Connection": "close"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode("utf-8", "replace").strip()
            except Exception as exc:
                last = exc
                if n + 1 < attempts:
                    time.sleep(0.6)
        raise last


def parse_status(body: str, key: str = ""):
    """
    Normalise whatever the appliance replied with.

    Returns (data, info) where info records the mode actually used, the
    detected appliance family and, for an encrypted reply, the recovered key.
    """
    info = {"encrypted": False, "type": None, "recovered_key": None}
    text = body.strip()

    if text and HEX_RE.match(text) and len(text) % 2 == 0 and not text.startswith("{"):
        info["encrypted"] = True
        used = key
        if not used:
            used = recover_key(text) or ""
            info["recovered_key"] = used or None
        if not used:
            raise ValueError("response is encrypted and the key could not be recovered")
        text = decrypt(text, used)

    # The firmware emits sloppy JSON: trailing commas, occasional NaN.
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    data = json.loads(cleaned)

    for root, family in STATUS_ROOTS.items():
        if root in data:
            info["type"] = family
            info["root"] = root
            break
    return data, info


def read_status(ip: str, key: str = "", timeout: float = TIMEOUT):
    """Read appliance status, preferring plaintext and falling back to encrypted."""
    errors = []
    for enc in (0, 1):
        url = "http://%s/http-read.json?encrypted=%d" % (ip, enc)
        try:
            body = _get(url, timeout)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            errors.append("%s: %s" % (url, exc))
            continue
        try:
            data, info = parse_status(body, key)
        except Exception as exc:
            errors.append("%s: %s" % (url, exc))
            continue
        info["url"] = url
        info["raw"] = body
        return data, info
    raise IOError("; ".join(errors) or "no response")


# --------------------------------------------------------------------------
# status decoding
#
# The enum values below are cross-checked against ofalvai/home-assistant-candy,
# an independent implementation of the same local API. Its machine-state and
# program-phase tables agree with what a real washer reports, and it confirms
# two easy-to-miss units: spin speed is rpm/100 and RemTime is in SECONDS.
# --------------------------------------------------------------------------

MACHINE_STATE = {
    1: "Idle",
    2: "Running",
    3: "Paused",
    4: "Delayed start selected",
    5: "Delayed start programmed",
    6: "Error",
    7: "Finished",
    8: "Finished",
}

WASH_PHASE = {
    0: "Stopped", 1: "Pre-wash", 2: "Wash", 3: "Rinse", 4: "Last rinse",
    5: "End", 6: "Drying", 7: "Error", 8: "Steam", 9: "Spin (good night)",
    10: "Spin",
}

DRY_LEVEL = {0: "No dry", 1: "Iron dry", 2: "Hang dry", 3: "Store dry", 4: "Bone dry"}


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def flatten_status(data: dict) -> dict:
    flat = {}
    for v in data.values():
        if isinstance(v, dict):
            flat.update(v)
    return flat


def decode_status(flat: dict) -> dict:
    """Turn the raw wire fields into something a person can read."""
    out = {}
    ms = _int(flat.get("MachMd"))
    if ms is not None:
        out["machine_state"] = MACHINE_STATE.get(ms, "Unknown (%d)" % ms)
        out["running"] = ms in (2, 3)
    ph = _int(flat.get("PrPh"))
    if ph is not None:
        out["phase"] = WASH_PHASE.get(ph, "Unknown (%d)" % ph)
    rem = _int(flat.get("RemTime"))
    if rem is not None:
        out["remaining_seconds"] = rem            # the field is already seconds
        out["remaining_minutes"] = rem // 60
    delay = _int(flat.get("DelVal"))
    if delay:
        out["delay_minutes"] = delay
    spin = _int(flat.get("SpinSp"))
    if spin is not None:
        out["spin_rpm"] = spin * 100
    temp = _int(flat.get("Temp"))
    if temp is not None:
        out["temperature_c"] = temp
    fill = _int(flat.get("FillR"))
    if fill is not None:
        out["fill_percent"] = fill
    dry = _int(flat.get("DryT"))
    if dry is not None:
        out["dry_level"] = DRY_LEVEL.get(dry, str(dry))
    # WiFiStatus is not link state: it is whether the panel is in remote mode,
    # and the appliance only accepts commands while it is 1.
    if "WiFiStatus" in flat or "StatoWiFi" in flat:
        out["remote_control"] = (flat.get("WiFiStatus") or flat.get("StatoWiFi")) == "1"
    err = _int(flat.get("Err"))
    if err is not None:
        out["error"] = None if err == 0 else err
    return out


STATS_SETTLE = 2.0        # seconds between prepareStatistics and getStatistics


def read_statistics(ip: str, key: str = "", timeout: float = TIMEOUT):
    """
    Read the appliance's lifetime counters.

    Useful beyond the counts themselves: the reply carries one ProgramN key per
    dial position, so counting them tells you how many programs the machine
    physically has — a fingerprint for identifying the model.
    """
    errors = []
    for enc in (0, 1):
        url = "http://%s/http-getStatistics.json?encrypted=%d" % (ip, enc)
        try:
            # Without a prepare first, the appliance answers with every counter
            # at 0. It needs a moment to fill them in afterwards.
            _get("http://%s/http-prepareStatistics.json?encrypted=%d" % (ip, enc), timeout)
            time.sleep(STATS_SETTLE)
            body = _get(url, timeout)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            errors.append("%s: %s" % (url, exc))
            continue
        if not body or "ERROR" in body[:40]:
            errors.append("%s: appliance returned an error" % url)
            continue
        try:
            data, info = parse_status(body, key)
        except Exception as exc:
            errors.append("%s: %s" % (url, exc))
            continue
        info["url"] = url
        return data, info
    raise IOError("; ".join(errors) or "no response")


def program_slots(stats: dict) -> int:
    """Count ProgramN counters in a statistics document -> number of dial positions."""
    flat = {}
    for v in stats.values():
        if isinstance(v, dict):
            flat.update(v)
    return sum(1 for k in flat if re.fullmatch(r"Program\d+", k))


def send_command(ip: str, params: str, key: str = "", timeout: float = TIMEOUT):
    """
    Send a write command. `params` is the raw query fragment the app builds,
    e.g. "Write=1&StSt=0&DelMd=0&PrNm=2".
    """
    if key:
        url = "http://%s/http-write.json?encrypted=1&data=%s" % (ip, encrypt(params, key))
    else:
        url = "http://%s/http-write.json?encrypted=0&%s" % (ip, params)
    return _get(url, timeout), url
