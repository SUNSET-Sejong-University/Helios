"""
Read, parse and live-plot ESP32-CSI from the Nano 33 IoT bridge.

Line format the firmware sends:
CSI,<rssi>,<channel>,<len>,<b0>,<b1>,...  (len signed int8 values)
The <len> bytes are interleaved (imag, real) pairs, so len/2 subcarriers.
Amplitude of a subcarrier = sqrt(real^2 + imag^2).

"""
import argparse
import time
import threading
from collections import deque

import numpy as np  
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import serial

# configuration
BAUS = 115200             
CALIB_PACKETS = 60     # packets used to learn which subcarriers are null/constant
WATERFALL_ROWS = 200   # how many recent packets the scrolling heatmap shows
SKIP_LEADING_PAIRS = 0 # bump to 1-2 if a constant header bar on the far left is visible

def parse_csi_line(line):
    """
    Return (rssi, channel, amp_array) or None if line is not a clean CSI record
    
    Everything that is not a well-formed CSI line -- boot messages, the SAMD21
    heartbeat, a heartbeat spliced into the middle of a packet, the partial first
    line at capture start -- returns None and gets skipped by the caller.
    
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split(",")
    if parts[0] != "CSI":
        return None
    try:
        rssi = int(parts[1])
        channel = int(parts[2])
        length = int(parts[3])
        vals = [int(x) for x in parts[4:]]
    except (ValueError, IndexError):
        return None
    if len(vals) != length:
        return None
    
    arr = np.asarray(vals, dtype=np.int8).astype(np.float64)
    imag = arr[0::2]   # even indices: imaginary
    real = arr[1::2]   # odd indices: real
    n = min(len(imag), len(real))
    if SKIP_LEADING_PAIRS:
        imag, real = imag[SKIP_LEADING_PAIRS:], real[SKIP_LEADING_PAIRS:]
    
    amp = np.hypot(real, imag)
    #phase = np.arctan2(imag, real)
    return rssi, channel, amp


def line_source(args):
    """Yield raw text lines from live serial."""
    ser = serial.Serial(args.port, BAUS, timeout=1)
    while True:
        raw = ser.readline()
        if raw:
            yield raw.decode("utf-8", errors="ignore")


class CSIStream:
    """Background reader: parses lines, learns the null mask, keeps a rolling waterfall."""
    
    def __init__(self, args):
        self.args = args
        self.lock =threading.Lock()
        self.n_sub = None
        self.null_mask = None
        self.waterfall = None
        self.latest_amp = None
        self.rssi = None
        self.channel = None
        self.count = 0
        self.vmax = 60.0
        self._calib = []
        self._recorded = [] if args.record else None
    
    def run(self):
        for line in line_source(self.args):
            parsed = parse_csi_line(line)
            if parsed is None:
                continue
            rssi, channel, amp = parsed
            with self.lock:
                self._ingest(rssi, channel, amp)
        
    def _ingest(self, rssi, channel, amp):
        if self.n_sub is None:  # locking the subcarrier count to the first good packet
            self.n_sub = len(amp)
            self.waterfall = np.zeros((WATERFALL_ROWS, self.n_sub))
        if len(amp) != self.n_sub:
            return              # a different-length packet -> skip so the matrix stays rectangular
        
        self.rssi, self.channel, self.count = rssi, channel, self.count + 1
        self.latest_amp = amp
        self.waterfall = np.roll(self.waterfall, -1, axis=0)
        self.waterfall[-1] = amp
        
        if len(self._calib) < CALIB_PACKETS:
            self._calib.append(amp)
            if len(self._calib) == CALIB_PACKETS:
                self._build_mask()
        
        if self._recorded is not None:
            self._recorded.append(amp)
    
    def _build_mask(self):
        M = np.asarray(self._calib)
        mean = M.mean(axis=0) 
        std = M.std(axis=0)
        # null subcarrier = ~zero everywhere; constant "header" = zero variance
        self.null_mask = (mean < 1.0) | (std < 1e-6)
        self.vmax = float(np.percentile(M[:, ~self.null_mask], 95)) or 60.0
        dropped = np.where(self.null_mask)[0].tolist()
        print(f"[calib] {self.n_sub} subcarriers -> dropping {len(dropped)} "
              f"null/constant, keeping {(~self.null_mask).sum()} active. ")
        print(f"[calib] dropped indices: {dropped}")

    def snapshot(self):
        """Thread-safe copy of what plotter needs."""
        with self.lock:
            if self.latest_amp is None:
                return None
            keep = ~self.null_mask if self.null_mask is not None else slice(None)
            return (self.latest_amp[keep].copy(),
                    self.waterfall[:, keep].copy(),
                    self.rssi, self.channel, self.count, self.vmax)
    
    def save(self):
        if self._recorded:
            out = np.asarray(self._recorded)
            np.save(self.args.record, out)
            print(f"[record] saved {out.shape[0]} packets x {out.shape[1]} "
                  f"subcarriers -> {self.args.record}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0", help="serial port")
    ap.add_argument("--record", default=None, help="save parsed amplitudes to this .npy file")
    args = ap.parse_args()

    stream = CSIStream(args)
    threading.Thread(target=stream.run, daemon=True).start()

    fig, (ax_line, ax_fall) = plt.subplots(
        2, 1, figsize=(9, 7), gridspec_kw={"height_ratios": [1, 2]})
    fig.canvas.manager.set_window_title("ESP32-CSI")

    (line,) = ax_line.plot([], [], lw=1.5)
    ax_line.set_title("current CSI amplitude across subcarriers")
    ax_line.set_xlabel("active subcarrier")   
    ax_line.set_ylabel("amplitude")

    im = ax_fall.imshow(np.zeros((WATERFALL_ROWS, 1)), aspect="auto",
                        origin="lower", interpolation="nearest", cmap="viridis") 
    ax_fall.set_title("waterfall (time flows upward -- move and watch it ripple)")
    ax_fall.set_xlabel("active subcarrier")
    ax_fall.set_ylabel("recent packets")

    def update(_):
        snap = stream.snapshot()
        if snap is None:
            return line, im
        amp, fall, rssi, ch, count, vmax = snap
        x = np.arange(len(amp))

        line.set_data(x, amp)
        ax_line.set_xlim(0, max(1, len(amp) - 1))
        ax_line.set_ylim(0, vmax * 1.3)

        im.set_data(fall)
        im.set_extent([0, fall.shape[1], 0, WATERFALL_ROWS])
        im.set_clim(0, vmax)

        status = "calibrating..." if stream.null_mask is None else "active"
        ax_line.set_title(f"CSI amplitude | rssi={rssi} ch={ch} "
                          f"packets={count} [{status}]")

        return line, im

    
    ani = animation.FuncAnimation(fig, update, interval=100, blit=False,
                                  cache_frame_data=False)
    plt.tight_layout()
    try:
        plt.show()
    finally:
        stream.save()


if __name__ == "__main__":
    main()