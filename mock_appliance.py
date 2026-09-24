"""
Mock simply-Fi appliance, for testing the local control app without hardware.

    python mock_appliance.py            plaintext, port 8080
    python mock_appliance.py --encrypt  encrypted with a random 16-char key

Point the web UI at  127.0.0.1:8080  (the IP field accepts host:port).
"""

import argparse
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import candy_protocol as cp

STATE = {
    "WiFiStatus": "1", "Err": "0", "MachMd": "1", "Pr": "0", "PrPh": "0",
    "PrCode": "65", "Temp": "40", "SpinSp": "08", "SLevel": "2", "Steam": "0",
    "DryT": "0", "DelVal": "0", "RemTime": "0", "FillR": "0",
    "Opt1": "0", "Opt2": "0", "CheckUpState": "0",
}

# When a cycle is started, RemTime counts down in real time so the UI's timer
# has something honest to track.
_started_at = None
_started_with = 0


def tick():
    """Advance the simulated cycle before answering a read."""
    global _started_at
    if _started_at is None or STATE["MachMd"] != "2":
        return
    left = _started_with - int(time.time() - _started_at)
    if left <= 0:
        STATE.update({"MachMd": "7", "PrPh": "5", "RemTime": "0", "FillR": "0"})
        _started_at = None
        return
    STATE["RemTime"] = str(left)
    done = 1 - left / float(_started_with or 1)
    STATE["PrPh"] = "1" if done < .1 else "2" if done < .6 else "3" if done < .85 else "10"
    STATE["FillR"] = str(int(40 * (1 - abs(.5 - done) * 2)))

KEY = ""


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[appliance] " + fmt % args)

    def reply(self, text):
        raw = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}

        if url.path == "/http-read.json":
            tick()
            body = '{"statusLavatrice":' + \
                   "{" + ",".join('"%s":"%s"' % kv for kv in STATE.items()) + "}}"
            if KEY and q.get("encrypted") == "1":
                return self.reply(cp.encrypt(body, KEY))
            if KEY:
                return self.reply("")          # refuses plaintext, like a keyed unit
            return self.reply(body)

        if url.path == "/http-write.json":
            if q.get("encrypted") == "1":
                params = cp.decrypt(q.get("data", ""), KEY) if KEY else ""
            else:
                params = url.query.split("&", 1)[1] if "&" in url.query else ""
            print("[appliance] COMMAND -> %s" % params)
            for pair in params.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k == "StSt":
                        global _started_at, _started_with
                        if v == "1":
                            STATE["MachMd"] = "2"
                            _started_with = int(os.environ.get("MOCK_CYCLE_SECONDS", "5400"))
                            _started_at = time.time()
                            STATE["RemTime"] = str(_started_with)
                        else:
                            STATE["MachMd"] = "1"
                            STATE["RemTime"] = "0"
                            _started_at = None
                    elif k in STATE:
                        STATE[k] = v
            return self.reply('{"response":"OK"}')

        self.send_error(404)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--encrypt", action="store_true")
    args = ap.parse_args()

    if args.encrypt:
        import random
        import string
        KEY = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
        print("mock appliance key: %s  (the app should recover this on its own)" % KEY)

    print("mock appliance on http://127.0.0.1:%d" % args.port)
    ThreadingHTTPServer(("127.0.0.1", args.port), Mock).serve_forever()
