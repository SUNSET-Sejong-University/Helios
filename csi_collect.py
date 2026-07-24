#!/usr/bin/env python3
"""
csi_collect.py -- record one labeled session of CSI to data/<label>__<n>.npz

Usage:
    python csi_collect.py --label walking --seconds 40
    python csi_collect.py --label empty   --seconds 40 --port /dev/ttyACM0

Record SEVERAL SEPARATE SESSIONS per label (3+ is the minimum that lets the
trainer honestly validate). Each run of this script = one session = one held-out
unit at training time. Stop, move around, restart -- that variation is the point.
"""

import argparse
import os
import time

import numpy as np
import serial

from csi_features import parse_csi_line

BAUD = 115200


def next_path(outdir, label):
    os.makedirs(outdir, exist_ok=True)
    n = 0
    while True:
        p = os.path.join(outdir, f"{label}__{n:02d}.npz")
        if not os.path.exists(p):
            return p, n
        n += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True,
                    help="activity name, e.g. empty / sitting / standing / walking")
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--countdown", type=int, default=5)
    args = ap.parse_args()

    path, session = next_path(args.outdir, args.label)
    ser = serial.Serial(args.port, BAUD, timeout=1)

    print(f"\nlabel     : {args.label}")
    print(f"session   : {session:02d}  ->  {path}")
    print(f"duration  : {args.seconds:.0f} s")
    print("\nGet into position. Recording starts in...")
    for i in range(args.countdown, 0, -1):
        print(f"  {i}")
        time.sleep(1)
    print("  RECORDING\n")

    amps, times, rssis = [], [], []
    n_sub = None
    prev = None
    t0 = time.time()
    last_report = t0

    while time.time() - t0 < args.seconds:
        raw = ser.readline()
        if not raw:
            continue
        parsed = parse_csi_line(raw.decode("utf-8", errors="ignore"))
        if parsed is None:
            continue
        rssi, channel, amp = parsed

        if n_sub is None:
            n_sub = len(amp)
        elif len(amp) != n_sub:
            continue  # keep the matrix rectangular

        amps.append(amp)
        times.append(time.time())
        rssis.append(rssi)

        # live feedback so you can see the rig responding while you record
        now = time.time()
        if prev is not None and now - last_report > 0.5:
            motion = float(np.mean(np.abs(amp - prev)))
            bar = "#" * min(40, int(motion * 6))
            left = args.seconds - (now - t0)
            print(f"\r  {left:5.1f}s  rssi {rssi:4d}  motion {motion:6.3f} {bar:<40}",
                  end="", flush=True)
            last_report = now
        prev = amp

    print("\n")
    if len(amps) < 20:
        print("!! too few packets captured -- is the stream running?")
        return

    amps = np.asarray(amps)
    times = np.asarray(times)
    span = times[-1] - times[0]
    fs = (len(times) - 1) / span if span > 0 else 0.0

    np.savez_compressed(
        path,
        amps=amps.astype(np.float32),      # (T, S) ALL subcarriers, mask applied later
        times=times,
        rssi=np.asarray(rssis, dtype=np.int16),
        label=args.label,
        session=session,
        fs=fs,
    )

    print(f"saved {path}")
    print(f"  packets   : {len(amps)}")
    print(f"  subcarrier: {amps.shape[1]} (raw, unmasked)")
    print(f"  rate      : {fs:.2f} Hz")
    print(f"  rssi      : {np.mean(rssis):.1f} dBm avg")
    print(f"\nRecord more sessions of '{args.label}', then other labels, then train.\n")


if __name__ == "__main__":
    main()