"""
Local control server for Candy / Hoover simply-Fi appliances.

Serves the web UI and proxies to the appliance on the LAN. The proxy exists
because the appliance sends no CORS headers and speaks plain HTTP, so a
browser cannot talk to it directly.

    python server.py            then open http://127.0.0.1:8099

Nothing leaves the local network.
"""

import json
import os
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import candy_protocol as cp

HERE = os.path.dirname(os.path.abspath(__file__))
BIND = os.environ.get("WASH_BIND", "127.0.0.1")
PORT = int(os.environ.get("WASH_PORT", "8099"))


def local_ipv4():
    """Address of the interface holding the default route (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_ipv4s():
    """
    Every IPv4 address this machine holds, not just the default-route one.

    A box on Wi-Fi plus Ethernet, or with a VPN up, has several — and guessing
    the wrong one is why a scan comes back with nothing.
    """
    ips = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(res[4][0])
    except OSError:
        pass
    ips.add(local_ipv4())
    return sorted(ip for ip in ips
                  if ip and not ip.startswith("127.") and not ip.startswith("169.254."))


def local_subnets():
    """Distinct /24 bases worth sweeping, default-route one first."""
    primary = ".".join(local_ipv4().split(".")[:3])
    bases = []
    for ip in local_ipv4s():
        b = ".".join(ip.split(".")[:3])
        if b not in bases:
            bases.append(b)
    bases.sort(key=lambda b: b != primary)
    return bases


def port_open(ip, port=80, timeout=0.35):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    finally:
        s.close()


def scan_subnet(base=None, progress=None):
    """
    Sweep for appliances: TCP/80 first, then an actual status read.

    With no base given, every local /24 is swept — a machine on more than one
    network otherwise silently scans the wrong one.
    """
    if base:
        # accept a comma-separated list, and tolerate full IPs
        bases = [".".join(b.strip().split(".")[:3])
                 for b in base.split(",") if b.strip()]
        bases = list(dict.fromkeys(bases))
    else:
        bases = local_subnets()
    hosts = ["%s.%d" % (b, i) for b in bases for i in range(1, 255)]

    with ThreadPoolExecutor(max_workers=128) as pool:
        open_hosts = [ip for ip, ok in zip(hosts, pool.map(port_open, hosts)) if ok]

    found = []

    def probe(ip):
        try:
            data, info = cp.read_status(ip, timeout=3.0)
        except Exception:
            return None
        return {
            "ip": ip,
            "type": info.get("type") or "unknown",
            "encrypted": info["encrypted"],
            "key": info.get("recovered_key") or "",
            "root": info.get("root"),
            "status": data,
        }

    with ThreadPoolExecutor(max_workers=32) as pool:
        for res in pool.map(probe, open_hosts):
            if res:
                found.append(res)
    return {"base": ", ".join(bases), "bases": bases, "scanned": len(hosts),
            "open": open_hosts, "appliances": found}


def load_programs():
    path = os.path.join(HERE, "programs.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


_ident_cache = {}
_IDENT_TTL = 600          # seconds; the model does not change under us


def identify(ip, key="", force=False):
    """
    Work out which model in the program database this appliance is.

    Two independent signals, neither of which needs the model number:
      * the statistics reply carries one ProgramN counter per dial position,
        so its length is the machine's program count;
      * the live PrCode must exist in the model's program set.
    """
    hit = _ident_cache.get(ip)
    if hit and not force and (time.time() - hit[0]) < _IDENT_TTL:
        return hit[1]

    result = {"ip": ip, "slots": None, "prcode": None, "candidates": [], "notes": []}

    data, info = cp.read_status(ip, key)
    flat = cp.flatten_status(data)
    result["type"] = info.get("type") or "unknown"
    result["prcode"] = flat.get("PrCode")
    result["pr"] = flat.get("Pr")

    try:
        stats, _ = cp.read_statistics(ip, key)
        result["slots"] = cp.program_slots(stats)
    except Exception as exc:
        result["notes"].append("statistics unavailable (%s)" % exc)

    db = load_programs()
    if not db:
        result["notes"].append("programs.json missing — run extract_programs.py")
        return result

    by_serial = {p["serial"]: p for p in db["programs"]}
    washer_fams = {"WA_PROG", "DUAL_WM_WD", "NFC_PROGRAM"}

    for m in db["models"]:
        progs = [by_serial[s] for s in m["programs"] if s in by_serial]
        real = [p for p in progs if p["family"] != "OFF"]
        fams = {p["family"] for p in real}

        score, why = 0, []
        if result["slots"]:
            if len(progs) == result["slots"]:
                score += 50
                why.append("program count %d matches exactly" % result["slots"])
            elif abs(len(progs) - result["slots"]) <= 1:
                score += 20
                why.append("program count %d is within one of %d"
                           % (len(progs), result["slots"]))
        if result["prcode"] is not None:
            if any(str(p.get("code")) == str(result["prcode"]) for p in real):
                score += 30
                why.append("has the live program code %s" % result["prcode"])
        if result["type"] in ("washer", "tumbledryer") and fams & washer_fams:
            score += 10
            why.append("washer-family programs")

        if score:
            result["candidates"].append({
                "id": m["id"], "count": len(progs), "families": sorted(fams),
                "score": score, "why": why,
            })

    result["candidates"].sort(key=lambda c: -c["score"])
    _ident_cache[ip] = (time.time(), result)
    return result


SETTINGS = os.path.join(HERE, "settings.json")


def read_settings():
    """App-wide settings, chiefly which appliance to open by default."""
    if not os.path.exists(SETTINGS):
        return {}
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        return {}


def write_settings(patch):
    s = read_settings()
    s.update({k: v for k, v in patch.items() if v is not None})
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=1, ensure_ascii=False)
    return s


def default_ip():
    return read_settings().get("ip", "")


PROFILES = os.path.join(HERE, "profiles.json")


def read_profiles():
    if not os.path.exists(PROFILES):
        return {}
    try:
        with open(PROFILES, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        return {}


def profile_key(ip, profiles=None):
    """Follow `same_as` so one machine that moves between DHCP addresses
    keeps a single profile, e.g. {"192.168.1.51": {"same_as": "192.168.1.50"}}."""
    profiles = read_profiles() if profiles is None else profiles
    seen = set()
    while ip not in seen and (profiles.get(ip) or {}).get("same_as"):
        seen.add(ip)
        ip = profiles[ip]["same_as"]
    return ip


def write_profile(ip, patch, reset=False):
    profiles = read_profiles()
    ip = profile_key(ip, profiles)
    prof = profiles.setdefault(ip, {"dial": {}})
    if "dial" in patch:
        if reset:
            prof["dial"] = patch.pop("dial")
        else:
            prof.setdefault("dial", {}).update(patch.pop("dial"))
    if "options" in patch:
        # bit number -> the name you gave it on the machine's own panel
        prof.setdefault("options", {}).update(patch.pop("options"))
    if "names" in patch:
        # program code -> the name printed on this machine's own panel
        prof.setdefault("names", {}).update(patch.pop("names"))
    prof.update(patch)
    with open(PROFILES, "w", encoding="utf-8") as fh:
        json.dump(profiles, fh, indent=1, ensure_ascii=False)
    return prof


CYCLES = os.path.join(HERE, "cycles.json")      # the cycle in progress, per appliance
HISTORY = os.path.join(HERE, "history.json")    # finished cycles + counter snapshots
_cycle_lock = threading.Lock()
_cycles = None            # canonical ip -> the cycle running on it
_quiet = {}               # canonical ip -> last time it was seen idle or asleep
_last_poll = {}           # canonical ip -> last status read, from any source
WATCH_EVERY = 60          # seconds between background checks of the default appliance
SNAPSHOT_EVERY = 6 * 3600 # seconds between lifetime-counter snapshots
HISTORY_MAX = 5000        # finished cycles kept
SNAPSHOT_MAX = 1500       # counter snapshots kept (about a year at 6-hourly)


def _read_json(path, empty):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return empty


def _write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _load_cycles():
    global _cycles
    if _cycles is None:
        _cycles = _read_json(CYCLES, {})
    return _cycles


def read_history():
    h = _read_json(HISTORY, {})
    h.setdefault("cycles", [])
    h.setdefault("snapshots", [])
    return h


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _finish(key, cur, now):
    """Move a finished cycle from cycles.json into the history log."""
    # a machine that went to sleep straight after finishing is noticed late:
    # it can't have ended later than its last reported time-left ran out
    ended = min(now, cur.get("seen", now) + (cur.get("left") or 0) + 120)
    entry = {k: cur.get(k) for k in ("code", "pos", "temp", "spin", "fill", "soil")}
    entry.update({"ip": key, "started": round(cur["started"]), "ended": round(ended),
                  "estimate": cur["total"], "from_start": cur["from_start"],
                  "duration": round(ended - cur["started"]) if cur["from_start"] else None})
    h = read_history()
    h["cycles"] = (h["cycles"] + [entry])[-HISTORY_MAX:]
    _write_json(HISTORY, h)


def track_cycle(ip, flat, dec):
    """
    Remember each cycle's full length, and log it once it finishes.

    The appliance only reports time left, never time elapsed, so the only way
    to know how far through a cycle is, is to have seen it start. `from_start`
    says whether we did: true when this machine was seen idle or asleep in the
    three minutes before the cycle first showed up. State survives restarts.
    """
    key = profile_key(ip)
    now = time.time()
    running, remaining = dec.get("running"), dec.get("remaining_seconds")
    code = flat.get("PrCode")
    with _cycle_lock:
        cycles = _load_cycles()
        _last_poll[key] = now
        cur = cycles.get(key)
        if not running or remaining is None:
            _quiet[key] = now
            if cur is not None:
                _finish(key, cycles.pop(key), now)
                _write_json(CYCLES, cycles)
            return None
        fresh = (cur is None or str(cur.get("code")) != str(code)
                 or now - cur.get("seen", 0) > 3 * 3600
                 or remaining > cur["total"] + 600)
        if fresh:
            if cur is not None:
                _finish(key, cur, now)
            cur = {"code": code, "pos": _num(flat.get("Pr")), "total": remaining,
                   "started": now, "from_start": now - _quiet.get(key, 0) <= 180,
                   "temp": dec.get("temperature_c"), "spin": dec.get("spin_rpm"),
                   "soil": _num(flat.get("SLevel")), "fill": dec.get("fill_percent")}
            cycles[key] = cur
        if cur.get("pos") is None:
            cur["pos"] = _num(flat.get("Pr"))
        cur["seen"], cur["left"] = now, remaining
        cur["total"] = max(cur["total"], remaining)
        # the load sensor settles during the first minutes; keep the peak
        if dec.get("fill_percent") is not None:
            cur["fill"] = max(cur.get("fill") or 0, dec["fill_percent"])
        _write_json(CYCLES, cycles)
        out = {"total_seconds": cur["total"], "from_start": cur["from_start"],
               "started": cur["started"]}
        if not cur["from_start"]:
            out["typical_seconds"] = typical_length(key, code)
        return out


def typical_length(key, code):
    """Median length of past cycles of this program on this machine that were
    seen from the start, for estimating progress when this one wasn't."""
    runs = sorted(c["duration"] for c in read_history()["cycles"]
                  if c.get("ip") == key and str(c.get("code")) == str(code) and c.get("duration"))
    return runs[len(runs) // 2] if runs else None


def _went_quiet(ip):
    """The appliance didn't answer. Close out a cycle that must have ended."""
    key = profile_key(ip)
    now = time.time()
    with _cycle_lock:
        _quiet[key] = _last_poll[key] = now
        cycles = _load_cycles()
        cur = cycles.get(key)
        if cur and now > cur.get("seen", now) + (cur.get("left") or 0) + 600:
            _finish(key, cycles.pop(key), now)
            _write_json(CYCLES, cycles)


def snapshot_counters(ip, stats):
    """Keep the lifetime counters over time, at most one snapshot per 6 hours."""
    flat = cp.flatten_status(stats)
    progs = {k[7:]: _num(v) for k, v in flat.items()
             if k.startswith("Program") and k[7:].isdigit()}
    if not any(progs.values()):
        return  # an all-zero reply means the prepare step didn't take
    key = profile_key(ip)
    with _cycle_lock:
        h = read_history()
        last = next((s for s in reversed(h["snapshots"]) if s.get("ip") == key), None)
        if last and time.time() - last["t"] < SNAPSHOT_EVERY and last["programs"] == progs:
            return
        h["snapshots"] = (h["snapshots"] + [{
            "ip": key, "t": round(time.time()), "programs": progs,
            "temps": {k: _num(flat.get(k)) for k in ("Temp0to30", "Temp40", "Temp60to90")},
        }])[-SNAPSHOT_MAX:]
        _write_json(HISTORY, h)


def watch_default():
    """Check the default appliance once a minute so a cycle's start (and end)
    is seen even when no page is open, and snapshot its counters every 6 hours.
    The minute check is skipped while a page is already polling."""
    last_snap = 0
    while True:
        time.sleep(WATCH_EVERY)
        ip = default_ip()
        if not ip:
            continue
        if time.time() - _last_poll.get(profile_key(ip), 0) >= WATCH_EVERY - 15:
            try:
                data, _ = cp.read_status(ip)
                flat = cp.flatten_status(data)
                track_cycle(ip, flat, cp.decode_status(flat))
            except Exception:
                _went_quiet(ip)
                continue
        if time.time() - last_snap >= SNAPSHOT_EVERY:
            try:
                stats, _ = cp.read_statistics(ip)
                snapshot_counters(ip, stats)
                last_snap = time.time()
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[server] " + fmt % args)

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, name, ctype):
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            return self._send(404, {"error": "%s missing" % name})
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        route = url.path

        try:
            if route in ("/", "/cycles", "/cycles.html"):
                return self._file("cycles.html", "text/html; charset=utf-8")

            if route in ("/stats", "/stats.html"):
                return self._file("stats.html", "text/html; charset=utf-8")

            if route in ("/advanced", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")

            if route == "/programs.json":
                return self._file("programs.json", "application/json")

            if route == "/api/hostinfo":
                ip = local_ipv4()
                return self._send(200, {
                    "ip": ip,
                    "base": ".".join(ip.split(".")[:3]),
                    "ips": local_ipv4s(),
                    "subnets": local_subnets(),
                })

            if route == "/api/scan":
                return self._send(200, scan_subnet(q.get("base") or None))

            if route == "/api/status":
                ip = q.get("ip") or default_ip()
                if not ip:
                    return self._send(400, {"error": "ip required"})
                data, info = cp.read_status(ip, q.get("key", ""))
                flat = cp.flatten_status(data)
                decoded = cp.decode_status(flat)
                return self._send(200, {
                    "status": data,
                    "decoded": decoded,
                    "cycle": track_cycle(ip, flat, decoded),
                    "type": info.get("type") or "unknown",
                    "encrypted": info["encrypted"],
                    "recovered_key": info.get("recovered_key") or "",
                    "root": info.get("root"),
                    "url": info.get("url"),
                    "raw": info.get("raw"),
                })

            if route == "/api/identify":
                ip = q.get("ip") or default_ip()
                if not ip:
                    return self._send(400, {"error": "ip required"})
                return self._send(200, identify(ip, q.get("key", ""),
                                                force=q.get("force") == "1"))

            if route == "/api/statistics":
                ip = q.get("ip") or default_ip()
                if not ip:
                    return self._send(400, {"error": "ip required"})
                data, info = cp.read_statistics(ip, q.get("key", ""))
                snapshot_counters(ip, data)
                return self._send(200, {"statistics": data,
                                        "slots": cp.program_slots(data),
                                        "url": info.get("url")})

            if route == "/api/history":
                ip = q.get("ip") or default_ip()
                key = profile_key(ip) if ip else None
                h = read_history()
                mine = lambda rows: [r for r in rows if not key or r.get("ip") == key]
                with _cycle_lock:
                    running = _load_cycles().get(key) if key else None
                return self._send(200, {"cycles": mine(h["cycles"]),
                                        "snapshots": mine(h["snapshots"]),
                                        "running": running})

            if route == "/api/appliances":
                db = load_programs() or {"models": []}
                names = {m["id"]: m.get("model") for m in db["models"]}
                out = []
                profiles = read_profiles()
                for ip in profiles:
                    prof = profiles.get(profile_key(ip, profiles), {})
                    out.append({"ip": ip,
                                "model": names.get(prof.get("model")) or "",
                                "name": prof.get("name") or ""})
                out.sort(key=lambda a: a["ip"])
                return self._send(200, {"appliances": out, "default": default_ip()})

            if route == "/api/settings":
                return self._send(200, read_settings())

            if route == "/api/settings/save":
                out = write_settings({"ip": q.get("ip"), "name": q.get("name")})
                return self._send(200, out)

            if route == "/api/settings/forget":
                s = read_settings()
                s.pop("ip", None)
                s.pop("name", None)
                with open(SETTINGS, "w", encoding="utf-8") as fh:
                    json.dump(s, fh, indent=1, ensure_ascii=False)
                return self._send(200, s)

            if route == "/api/profile":
                ip = q.get("ip")
                profiles = read_profiles()
                if not ip:
                    return self._send(200, profiles)
                return self._send(200, profiles.get(profile_key(ip, profiles), {"dial": {}}))

            if route == "/api/profile/save":
                ip = q.get("ip")
                if not ip:
                    return self._send(400, {"error": "ip required"})
                patch = {}
                if q.get("model"):
                    patch["model"] = q["model"]
                if q.get("name"):
                    patch["name"] = q["name"]
                if q.get("dial"):
                    try:
                        patch["dial"] = json.loads(q["dial"])
                    except ValueError:
                        return self._send(400, {"error": "dial must be JSON"})
                if q.get("options"):
                    try:
                        patch["options"] = json.loads(q["options"])
                    except ValueError:
                        return self._send(400, {"error": "options must be JSON"})
                if q.get("names"):
                    try:
                        patch["names"] = json.loads(q["names"])
                    except ValueError:
                        return self._send(400, {"error": "names must be JSON"})
                return self._send(200, write_profile(ip, patch,
                                                     reset=q.get("reset") == "1"))

            if route == "/api/command":
                ip, params = q.get("ip") or default_ip(), q.get("params")
                if not ip or not params:
                    return self._send(400, {"error": "ip and params required"})
                body, sent = cp.send_command(ip, params, q.get("key", ""))
                return self._send(200, {"sent": sent, "params": params, "response": body})

            return self._send(404, {"error": "no such route"})

        except Exception as exc:
            text = str(exc)
            # These appliances drop their Wi-Fi when idle, so a timeout is the
            # normal "not awake" case rather than something the user broke.
            if "timed out" in text or "unreachable" in text or "refused" in text:
                friendly = ("The appliance did not answer. These machines put their "
                            "Wi-Fi to sleep when they have been idle a while — "
                            "try again, or press a button on the machine to wake it.")
                return self._send(502, {"error": friendly, "detail": text,
                                        "unreachable": True})
            return self._send(502, {"error": "%s: %s" % (type(exc).__name__, exc),
                                    "detail": text})


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    threading.Thread(target=watch_default, daemon=True).start()
    ip = local_ipv4()
    print("Candy/Hoover local control")
    print("  this machine : %s" % ip)
    print("  open         : http://%s:%d" % (BIND, PORT))
    print("  Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.shutdown()


if __name__ == "__main__":
    main()
