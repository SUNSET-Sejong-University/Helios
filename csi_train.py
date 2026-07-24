#!/usr/bin/env python3
"""
csi_train.py -- train the activity classifier and validate it honestly.

Usage:
    pip install scikit-learn joblib
    python csi_train.py --data data --out model.joblib

Why leave-one-session-out:
    Windows overlap by 75%, so neighbouring windows are near-duplicates. A random
    train/test split would put near-copies of the same moment on both sides and
    report ~99% accuracy for a model that generalises to nothing. This script
    instead holds out WHOLE SESSIONS, so the test data comes from a recording the
    model has never seen. The number it prints is the number you can defend.
"""

import argparse
import glob
import os
from collections import Counter, defaultdict

import numpy as np
import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import LeaveOneGroupOut

from csi_features import (WINDOW_SEC, HOP_SEC, build_null_mask, make_windows,
                          extract_features, feature_names)

# maps raw label -> training label; anything not listed keeps its own name
DEFAULT_MERGE = {
    "sitting":  "still",
    "standing": "still",
    "walking":  "walking",
}


def load_sessions(datadir):
    files = sorted(glob.glob(os.path.join(datadir, "*.npz")))
    if not files:
        raise SystemExit(f"no .npz files in {datadir}/ -- run csi_collect.py first")
    sessions = []
    for f in files:
        z = np.load(f, allow_pickle=True)
        sessions.append({
            "file": os.path.basename(f),
            "amps": z["amps"].astype(float),
            "label": str(z["label"]),
            "fs": float(z["fs"]),
            "group": os.path.basename(f),      # one session = one held-out group
        })
    return sessions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="model.joblib")
    ap.add_argument("--trees", type=int, default=300)
    ap.add_argument("--merge", action="store_true",
                help="collapse sitting+standing into a single 'still' class")
    args = ap.parse_args()

    sessions = load_sessions(args.data)
    if args.merge:
        for s in sessions:
            s["label"] = DEFAULT_MERGE.get(s["label"], s["label"])
        print("[merge] sitting+standing -> still")

    # ---- sanity: shape + session count per label ----
    widths = {s["amps"].shape[1] for s in sessions}
    if len(widths) > 1:
        raise SystemExit(f"sessions have different subcarrier counts {widths} -- "
                         "these were captured with different settings, don't mix them")

    per_label = defaultdict(list)
    for s in sessions:
        per_label[s["label"]].append(s["file"])

    print("sessions found:")
    for lab, fs_ in sorted(per_label.items()):
        print(f"  {lab:<12} {len(fs_)} session(s)")
    thin = [l for l, f in per_label.items() if len(f) < 2]
    if thin:
        print(f"\n!! {', '.join(thin)} has <2 sessions -- cannot validate it honestly.")
        print("   Record at least 2-3 separate sessions per label and rerun.\n")
    if len(per_label) < 2:
        raise SystemExit("need at least 2 different labels to train a classifier")

    # ---- global null mask across ALL sessions (so every window aligns) ----
    mask = build_null_mask(np.vstack([s["amps"] for s in sessions]))
    keep = ~mask
    n_sub = int(keep.sum())
    print(f"\nsubcarriers: {len(mask)} raw -> {n_sub} active "
          f"({int(mask.sum())} null/constant dropped)")

    # ---- windows -> features ----
    X, y, groups = [], [], []
    for s in sessions:
        act = s["amps"][:, keep]
        fs = s["fs"] if s["fs"] > 1 else 10.0
        wins = make_windows(act, fs)
        for win, _ in wins:
            X.append(extract_features(win))
            y.append(s["label"])
            groups.append(s["group"])
    X = np.asarray(X)
    y = np.asarray(y)
    groups = np.asarray(groups)

    print(f"windows    : {len(X)} of {WINDOW_SEC}s (hop {HOP_SEC}s)")
    print(f"features   : {X.shape[1]}")
    print("class balance:", dict(Counter(y)))

    # ---- leave-one-session-out cross validation ----
    print("\n--- leave-one-session-out validation ---")
    logo = LeaveOneGroupOut()
    y_true_all, y_pred_all = [], []
    for tr, te in logo.split(X, y, groups):
        if len(set(y[tr])) < 2:
            continue
        clf = RandomForestClassifier(n_estimators=args.trees, random_state=0,
                                     class_weight="balanced_subsample", n_jobs=-1)
        clf.fit(X[tr], y[tr])
        pred = clf.predict(X[te])
        y_true_all.append(y[te])
        y_pred_all.append(pred)
        held = groups[te][0]
        acc = float((pred == y[te]).mean())
        print(f"  held out {held:<22} acc {acc:5.1%}")

    y_true_all = np.concatenate(y_true_all)
    y_pred_all = np.concatenate(y_pred_all)
    overall = float((y_true_all == y_pred_all).mean())

    labels = sorted(set(y))
    print(f"\noverall held-out accuracy: {overall:.1%}")
    print("\nconfusion matrix (rows = truth, cols = predicted)")
    cm = confusion_matrix(y_true_all, y_pred_all, labels=labels)
    print("  " + "".join(f"{l[:7]:>9}" for l in labels))
    for l, row in zip(labels, cm):
        print(f"{l[:10]:<10}" + "".join(f"{v:>9}" for v in row))
    print("\n" + classification_report(y_true_all, y_pred_all, digits=3))

    # ---- final model on everything ----
    final = RandomForestClassifier(n_estimators=args.trees, random_state=0,
                                   class_weight="balanced_subsample", n_jobs=-1)
    final.fit(X, y)

    names = feature_names(n_sub)
    imp = sorted(zip(names, final.feature_importances_), key=lambda t: -t[1])[:10]
    print("top features:")
    for nme, v in imp:
        print(f"  {nme:<16} {v:.4f}")

    joblib.dump({
        "model": final,
        "null_mask": mask,
        "classes": list(final.classes_),
        "window_sec": WINDOW_SEC,
        "hop_sec": HOP_SEC,
        "n_sub": n_sub,
        "holdout_accuracy": overall,
    }, args.out)
    print(f"\nsaved {args.out}")

    if overall > 0.97:
        print("\n!! >97% held-out accuracy is suspicious for single-link CSI.")
        print("   Check that your sessions really are separate recordings and that")
        print("   each class wasn't recorded in one continuous block per label.")


if __name__ == "__main__":
    main()