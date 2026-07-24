# HELIOS: WiFi CSI Activity Sensing on the Arduino Nano 33 IoT

A single-link WiFi **Channel State Information (CSI)** sensing system that classifies
human activity (`empty` / `still` / `walking`) from the WiFi channel, gated by a PIR
motion sensor, with results published over the **oneM2M** IoT service layer and shown
on a live dashboard.


The system repurposes the u-blox NINA-W102 module inside an Arduino Nano 33 IoT — which
houses a standard ESP32 — as a raw CSI extractor, something the stock `WiFiNINA`
library does not expose.

> **Scope, stated honestly.** This is *activity classification*, not joint-level pose
> reconstruction. A single antenna has no spatial diversity, so a skeleton/DensePose
> output is physically out of reach on this hardware (see *Limitations*). What one link
> does well — distinguishing motion from stillness and presence — is exactly what this
> system delivers.


## Motivation
 
The goal is a **privacy-preserving, camera-free room monitor** for the local network.
An owner leaving a room (or home) cannot watch it while away — but a camera is intrusive,
records identifiable footage, and is itself a privacy and security liability. This system
answers the practical question *"did someone enter my room while I was out, and what were
they doing?"* **without any camera**.
 
WiFi is the sensing medium instead of a lens: a person entering the room perturbs the
WiFi channel, and that perturbation is logged as discrete activity events. When the owner
returns, they query the event history over the local network and see whether the room was
entered, when, and whether the intruder was moving around or merely passing through — all
without a single image being captured.
 
This shapes every design decision:
 
- **PIR + CSI cascade** — the PIR cheaply flags *entry* (motion); CSI then logs *what
  happened* (moving vs. still presence), which a PIR alone cannot report once motion stops.
- **oneM2M as the log** — activity and presence events are stored as timestamped records
  in a local CSE, so the owner (or any authorised app on the network) can review the
  history after the fact rather than needing to watch a live feed.
- **Camera-free by construction** — the raw signal never leaves the local machine, and no
  visual data of any kind is produced; the only output is an abstract activity label.
  
<br>
<img width="1349" height="647" alt="image" src="https://github.com/user-attachments/assets/3046b779-ba5b-4036-9fb7-197cf32567dd" />

---

## What it does

- Captures WiFi CSI on an ESP32 (inside the Nano's NINA module) and streams it to a PC.
- A PIR sensor detects presence; **only when someone is present** is CSI classified.
- A Random-Forest classifier labels 2-second windows as `empty` / `still` / `walking`.
- Classifications and presence events are posted to a **oneM2M CSE** (ACME) as the
  control plane; the raw CSI stays on a **local websocket** as the data plane.
- A browser dashboard reads the classification **from the CSE** and the raw waterfall
  from the websocket.

## Architecture at a glance

```
  ESP32 (NINA)            SAMD21 bridge            Laptop (Python)                  Browser
 ┌────────────┐  UART    ┌────────────┐  USB       ┌───────────────────┐           ┌──────────┐
 │ CSI extract│─────────▶│ passthrough│──────────▶ │ csi_ingest_ae.py  │           │ ruview   │
 │ + ping AP  │ 115200   │ + PIR read │  CSI,...   │                   │           │  .html   │
 └────────────┘          └─────┬──────┘  PIR,x     │  classify (gated) │─┐  HTTP   │          │
                               │                   │      │            │ ├───────▶ │ activity │
                          PIR ─┘                   │      ▼            │ │ (poll)  │ presence │
                                                   │  oneM2M CSE (ACME)│◀┘         │          │
                                                   │      │            │           │          │
                                                   │  raw CSI ─────────┼──────────▶│ waterfall│
                                                   └───────────────────┘   ws      └──────────┘
                                                                          (local, not via oneM2M)
```

## Hardware

| Part | Role |
|------|------|
| Arduino Nano 33 IoT | SAMD21 (bridge to PC) + u-blox NINA-W102 (ESP32-D0WDQ6, 2 MB flash) |
| HC-SR501 PIR sensor | Presence trigger, wired to SAMD21 pin **D2** |
| Host laptop | Runs the classifier, the oneM2M CSE, and serves the dashboard |

PIR wiring: `VCC → 5V/VUSB`, `GND → GND`, `OUT → D2`. The HC-SR501 output is 3.3 V logic
(safe for the SAMD21); power it from 5 V, **not** 3.3 V.

<table>
  <tr>
    <td><img src="https://github.com/user-attachments/assets/32852364-402f-4385-883d-cf7df59f3b9b" width="100%"></td>
    <td><img src="https://github.com/user-attachments/assets/1e840987-dbb1-4faa-aee7-ee5827b6bfb4" width="100%"></td>
  </tr>
  <tr>
    <td align="center"><em>Nano 33 IoT (NINA/ESP32 + SAMD21) beside the HC-SR501 PIR</em></td>
    <td align="center"><em>PIR wired to the Nano: VCC, OUT→D2, GND</em></td>
  </tr>
</table>

## Quick start

```bash
# 1. Python deps
pip install pyserial numpy scikit-learn joblib websockets

# 2. oneM2M CSE (ACME)
pip install acmecse
cd cse && acmecse            # accept Development defaults; add [http.cors] enable=true

# 3. Flash the boards (see DOCUMENTATION.md for the full procedure)
#    - ESP32:  CSI_Extractor.ino   (via Arduino IDE, ESP32 Dev Module / 2MB / Minimal / DIO)
#    - SAMD21: bridge_with_pir.ino (Nano 33 IoT board type)

# 4. Run the pipeline
python csi_ingest_ae.py --port /dev/ttyACM0 --model model_still.joblib --cse http://localhost:8080

# 5. Open ruview.html in a browser (or serve it: python -m http.server 9000)
```

To (re)train the classifier from scratch, see the *Training* section of `DOCUMENTATION.md`.

## Files

| File | Purpose |
|------|---------|
| `CSI_Extractor.ino` | ESP32 firmware: watchdog-safe CSI capture, associate + ping, ASCII output |
| `bridge_with_pir.ino` | SAMD21 firmware: CSI passthrough + PIR reading (boundary-safe) |
| `SerialNINAPassthrough` | Stock sketch used only to flash the ESP32 through the SAMD21 |
| `csi_features.py` | Shared parsing, null masking, windowing, feature extraction |
| `csi_collect.py` | Records labeled CSI sessions to `data/*.npz` |
| `csi_train.py` | Trains the classifier with leave-one-session-out validation |
| `csi_parser.py` | Standalone live matplotlib viewer (debugging / inspection) |
| `csi_metrics.py` | Websocket metrics engine (earlier, non-oneM2M dashboard path) |
| `onem2m.py` | Minimal oneM2M HTTP client (AE / container / contentInstance) |
| `csi_ingest_ae.py` | **Main runtime**: serial → classify → CSE, and raw CSI → websocket |
| `ruview.html` | Live dashboard (reads classification from the CSE, waterfall from websocket) |
| `model_still.joblib` | Trained 3-class model (empty / still / walking) |
| `model_4class.joblib` | 4-class model (empty / sitting / standing / walking) — limit study |

## Results

Validated with **leave-one-session-out** cross-validation (each recording session held
out entirely, so overlapping windows cannot leak between train and test).

| Model | Classes | Held-out accuracy |
|-------|---------|-------------------|
| `model_still.joblib` | empty / still / walking | **98.4%** |
| `model_4class.joblib` | empty / sitting / standing / walking | 86.2% |

The gap between the two is the key finding: merging `sitting` + `standing` into `still`
jumps accuracy by ~12 points, because the residual error in the 4-class model is almost
entirely `sitting`↔`standing` confusion — two *static* postures a single antenna cannot
separate. The system distinguishes **motion from stillness** near-perfectly; it cannot
distinguish two motionless postures.

## Limitations

- **No skeleton / pose reconstruction.** One antenna = no angular resolution; 40 MHz
  bandwidth = ~3.75 m range resolution (a whole person fits in one range bin). Joint-level
  pose needs the multi-antenna spatial diversity (e.g. 3×3 MIMO) this hardware lacks.
- **Room- and session-specific.** The model is trained for one physical layout; moving the
  router or board requires retraining.
- **Software-gated, not power-gated.** The ESP32 streams continuously; the PIR gates whether
  the laptop *acts* on the stream, not whether the radio runs.
- **Presence semantics.** PIR detects *motion-based* presence; a perfectly still person can
  fall below its threshold. The CSI `still` class complements this.
- **Vital signs out of scope.** Heart rate is not recoverable on a single 8-bit link;
  respiration is redundant with `still` and unreliable under motion — both excluded by design.
- **Does not identify individuals, by design.** The system reports *presence and activity*
  (entered / still / moving), never *who*. Identity inference is out of scope and
  intentionally so — for a privacy-preserving monitor, not identifying the person is a
  feature, not a gap.

## Acknowledgements

Built on the ESP32 CSI capability exposed by the Espressif `esp_wifi` API, the ACME oneM2M
CSE (ankraft), and standard Python scientific libraries (NumPy, scikit-learn, Matplotlib).
