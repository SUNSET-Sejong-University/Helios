#!/usr/bin/env python3
"""
csi_features.py -- shared parsing / masking / windowing / feature extraction.

Imported by csi_collect.py and csi_train.py so that the features used at training
time and at inference time can never drift apart. If you change anything here,
retrain the model.
"""

import numpy as np

WINDOW_SEC = 2.0     # length of one classified window
HOP_SEC = 0.5        # step between windows (75% overlap -> more training samples)


# ---------------------------------------------------------------- parsing

def parse_csi_line(line):
    """Return (rssi, channel, amp_all) or None.

    amp_all keeps EVERY subcarrier, including nulls. The mask is applied later,
    globally, so that every recording session uses an identical subcarrier set.
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
    imag, real = arr[0::2], arr[1::2]
    n = min(len(imag), len(real))
    return rssi, channel, np.hypot(real[:n], imag[:n])


# ---------------------------------------------------------------- masking

def build_null_mask(all_amps):
    """all_amps: (N, S) stacked from EVERY session. Returns a bool mask of
    subcarriers to DROP: nulls (~zero always) and constants (zero variance)."""
    M = np.asarray(all_amps, dtype=float)
    return (M.mean(axis=0) < 1.0) | (M.std(axis=0) < 1e-6)


# ---------------------------------------------------------------- windowing

def make_windows(amps, fs, window_sec=WINDOW_SEC, hop_sec=HOP_SEC):
    """amps: (T, S) active subcarriers. Yields (win, start_index)."""
    w = max(4, int(round(window_sec * fs)))
    h = max(1, int(round(hop_sec * fs)))
    out = []
    for s in range(0, len(amps) - w + 1, h):
        out.append((amps[s:s + w], s))
    return out


# ---------------------------------------------------------------- features

def extract_features(win):
    """win: (T, S) amplitudes for one window -> 1-D feature vector.

    Design notes:
      * The window is divided by its own mean amplitude. This removes slow gain
        drift / distance changes and keeps the *shape* of the channel, so the
        model can't cheat by memorising absolute signal power.
      * Absolute power is then re-added as ONE explicit feature, so it's available
        but can't dominate.
      * First-difference features carry the motion; static-shape features carry
        posture. Both matter.
    """
    win = np.asarray(win, dtype=float)
    scale = float(win.mean()) + 1e-9
    w = win / scale

    d = np.abs(np.diff(w, axis=0))          # (T-1, S) packet-to-packet change

    per_sub_mean = w.mean(axis=0)           # S : channel shape
    per_sub_std = w.std(axis=0)             # S : per-carrier fluctuation
    per_sub_motion = d.mean(axis=0)         # S : per-carrier motion

    # spectral shape of the aggregate signal (gait rhythm shows up here)
    m = w.mean(axis=1)
    m = m - m.mean()
    if len(m) >= 8:
        X = np.abs(np.fft.rfft(m * np.hanning(len(m))))
        X = X[1:]                           # drop DC
        tot = X.sum() + 1e-9
        spec = [
            float(np.argmax(X)),            # dominant bin
            float(X.max() / tot),           # peakiness
            float(X[:len(X) // 3].sum() / tot),   # low-freq energy fraction
        ]
    else:
        spec = [0.0, 0.0, 0.0]

    glob = [
        scale,                              # absolute power (one feature only)
        float(w.std()),
        float(d.mean()), float(d.std()), float(d.max()),
        float(np.percentile(d, 90)),
        float(per_sub_std.mean()), float(per_sub_std.max()),
        float(per_sub_motion.mean()), float(per_sub_motion.max()),
        float(np.corrcoef(w[0], w[-1])[0, 1]) if w.shape[1] > 2 else 0.0,
    ]

    return np.concatenate([per_sub_mean, per_sub_std, per_sub_motion,
                           np.asarray(spec), np.asarray(glob)])


def feature_names(n_sub):
    names = []
    for tag in ("mean", "std", "motion"):
        names += [f"sub{i}_{tag}" for i in range(n_sub)]
    names += ["spec_dom", "spec_peak", "spec_low"]
    names += ["power", "w_std", "d_mean", "d_std", "d_max", "d_p90",
              "substd_mean", "substd_max", "submot_mean", "submot_max",
              "shape_corr"]
    return names