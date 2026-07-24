#!/usr/bin/env python3
"""
csi_ingest_ae.py -- laptop-side oneM2M ingest/control AE + raw-CSI websocket.

Two OUTPUTS from one serial reader (so nothing fights for /dev/ttyACM0):

  1. CLASSIFICATION -> oneM2M CSE   (control plane, low-rate, the managed signal)
        - PIR edges  -> presence contentInstance
        - activity   -> activity contentInstance  (only while PIR present)
     RuView reads these back from the CSE.

  2. RAW CSI       -> websocket     (data plane, high-rate, LOCAL ONLY)
        - each packet's active-subcarrier amplitudes streamed to the browser
     RuView draws the waterfall from this. Deliberately NOT via oneM2M: the raw
     firehose stays on a direct local path; the service layer only carries the
     derived decision.

Run:
    pip install pyserial numpy scikit-learn joblib websockets
    # start acmecse first, then:
    python csi_ingest_ae.py --port /dev/ttyACM0 --model model_still.joblib \
                            --cse http://localhost:8080
"""

import argparse
import asyncio
import json
import threading
import time
from collections import deque

import numpy as np
import serial
import joblib
import websockets

from csi_features import parse_csi_line, extract_features, WINDOW_SEC
from onem2m import OneM2M

BAUD = 115200
PREDICT_EVERY = 5          # classify every Nth CSI packet while present
WS_HZ = 12                 # raw waterfall publish rate


class Ingest:
    """Owns the serial port. Writes classification to the CSE and exposes the
    latest raw amplitude vector for the websocket."""

    def __init__(self, port, model_path, m, aei):
        self.port = port
        self.m = m
        self.aei = aei

        bundle = joblib.load(model_path)
        self.null_mask = np.asarray(bundle["null_mask"], dtype=bool)
        self.classes = list(bundle["classes"])
        self.clf = bundle["model"]
        print(f"[model] {model_path} classes={self.classes} "
              f"({int((~self.null_mask).sum())} active subcarriers)")

        self.present = False
        self.hist = deque(maxlen=400)
        self.times = deque(maxlen=400)
        self.since_pred = 0
        self.count = 0

        # shared with the websocket pump
        self.lock = threading.Lock()
        self.latest_amp = None
        self.posture = None
        self.posture_conf = 0.0

    def _fs(self):
        if len(self.times) < 10:
            return 10.0
        span = self.times[-1] - self.times[0]
        return (len(self.times) - 1) / span if span > 0 else 10.0

    def run(self):
        ser = serial.Serial(self.port, BAUD, timeout=1)
        print("[serial] reading CSI + PIR ... (waiting for PIR trigger to classify)")
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()

            # ---- PIR edge: presence control-plane event ----
            if line.startswith("PIR,"):
                try:
                    val = int(line.split(",")[1])
                except (IndexError, ValueError):
                    continue
                new_present = bool(val)
                if new_present != self.present:
                    self.present = new_present
                    self.m.post_cin("wifi-sensing/presence",
                                    {"present": val, "ts": round(time.time(), 2)},
                                    originator=self.aei)
                    print(f"[pir] presence -> {self.present}  (posted to CSE)")
                    if not self.present:
                        with self.lock:
                            self.hist.clear(); self.times.clear()
                            self.posture = None
                continue

            # ---- CSI packet ----
            parsed = parse_csi_line(line)
            if parsed is None:
                continue
            _rssi, _ch, amp = parsed
            if len(amp) != len(self.null_mask):
                continue
            act = amp[~self.null_mask]

            with self.lock:
                self.latest_amp = act          # for the websocket (always, even if absent)
                self.count += 1
            self.hist.append(act)
            self.times.append(time.time())

            # ---- GATE: classify + publish to CSE only while present ----
            if not self.present:
                continue
            self.since_pred += 1
            w = max(4, int(round(WINDOW_SEC * self._fs())))
            if len(self.hist) < w or self.since_pred < PREDICT_EVERY:
                continue
            self.since_pred = 0

            win = np.asarray(list(self.hist)[-w:])
            proba = self.clf.predict_proba(extract_features(win).reshape(1, -1))[0]
            i = int(proba.argmax())
            label, conf = self.classes[i], float(proba[i])
            with self.lock:
                self.posture, self.posture_conf = label, conf
            self.m.post_cin("wifi-sensing/activity",
                            {"activity": label, "confidence": round(conf, 2),
                             "ts": round(time.time(), 2)}, originator=self.aei)
            print(f"[activity] {label:<8} conf {conf:.2f}  -> CSE")

    def raw_snapshot(self):
        with self.lock:
            if self.latest_amp is None:
                return None
            return {
                "amp": [round(float(v), 1) for v in self.latest_amp],
                "present": self.present,
                "posture": self.posture,
                "posture_conf": round(self.posture_conf, 2),
                "fs": round(self._fs(), 1),
                "count": self.count,
            }


async def ws_serve(ingest, host, port):
    clients = set()

    async def handler(ws, path=None):
        clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    async def pump():
        while True:
            snap = ingest.raw_snapshot()
            if clients and snap is not None:
                msg = json.dumps(snap)
                await asyncio.gather(*[c.send(msg) for c in list(clients)],
                                     return_exceptions=True)
            await asyncio.sleep(1.0 / WS_HZ)

    try:
        server = await websockets.serve(handler, host, port)
    except OSError:
        print(f"\n[ws] ERROR: port {port} is already in use.")
        print(f"[ws] A previous csi_ingest_ae.py is probably still running. Clear it:")
        print(f"[ws]     pkill -f csi_ingest_ae.py   (wait 3s, then retry)")
        print(f"[ws] or free the port:  kill $(lsof -t -i :{port})\n")
        return
    async with server:
        print(f"[ws] raw CSI (local, NOT via oneM2M) on ws://{host}:{port}")
        await pump()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--model", default="model_still.joblib")
    ap.add_argument("--cse", default="http://localhost:8080")
    ap.add_argument("--cse-name", default="cse-in")
    ap.add_argument("--ws-host", default="localhost")
    ap.add_argument("--ws-port", type=int, default=8765)
    args = ap.parse_args()

    # ---- oneM2M setup: register AE, ensure containers ----
    m = OneM2M(args.cse, cse_name=args.cse_name)
    aei = m.ensure_ae("wifi-sensing")
    m.originator = aei
    m.ensure_container("wifi-sensing", "presence", mni=60, originator=aei)
    m.ensure_container("wifi-sensing", "activity", mni=120, originator=aei)
    print(f"[onem2m] AE registered aei={aei}; containers presence/activity ready")

    ingest = Ingest(args.port, args.model, m, aei)
    threading.Thread(target=ingest.run, daemon=True).start()
    asyncio.run(ws_serve(ingest, args.ws_host, args.ws_port))


if __name__ == "__main__":
    main()