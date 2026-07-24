# Technical Documentation — HELIOS

This document explains the theory, architecture, and implementation of the system in
depth, and records the setup procedure, design decisions, and known limitations. For a
quick overview and file manifest, see `README.md`.

---

## 0. Purpose
 
The system is a **camera-free room monitor** for a local network. It answers, after the
fact, *"was my room entered while I was away, when, and was the person moving around or
just passing through?"* — using WiFi channel perturbations as the sensing medium instead
of a camera. No images are ever captured; the raw signal stays on the local machine, and
the only persisted output is an abstract, timestamped activity label held in a local
oneM2M CSE that the owner queries on return. This purpose is why the design pairs a cheap
PIR entry-trigger with CSI activity logging, and why the event history lives in a
standardised, locally-hosted service layer rather than a video feed.
 
---

## 1. Background: what CSI is and why it senses people

WiFi does not travel in a single clean line from transmitter to receiver. The signal
reflects off walls, floor, furniture, and any human body in the room, so the receiver
hears many delayed, attenuated copies of the same signal superimposed — **multipath**.

Modern WiFi (OFDM) splits the channel into dozens of narrow **subcarriers**, each a
slightly different frequency. Multipath affects each subcarrier differently. To decode a
packet, the receiver measures, per subcarrier, how the channel altered amplitude and
phase — using a known **training field** in every packet's preamble. That per-subcarrier
set of complex measurements *is* the Channel State Information (CSI): the `H` in
`received = H × transmitted + noise`.

When a person moves, the reflected paths change, so `H` changes. Tracking how CSI evolves
over time is therefore a low-resolution motion sensor built from a measurement WiFi
already performs for free. The ESP32 is unusual in letting firmware **keep** the CSI that
every other receiver computes and discards.

### CSI data layout on this hardware

- Each captured packet yields a buffer of `len` signed 8-bit integers.
- On this setup `len = 384`, stored as **interleaved (imaginary, real) pairs** →
  192 subcarrier slots.
- The 384 bytes are three 64-subcarrier blocks (LLTF, HT-LTF, and STBC-HT-LTF, the last
  because `stbc_htltf2_en` is enabled in the CSI config).
- Per subcarrier: `amplitude = sqrt(real² + imag²)`, `phase = atan2(imag, real)`.
- Many subcarriers are **null** (guard bands, DC) and read ~0 in every packet; a few
  leading bytes are constant header artifacts. Both are removed by the null mask
  (see §5.2), leaving ~169 informative subcarriers.

This project uses **amplitude only**. Raw CSI phase carries a random per-packet offset and
needs sanitisation before use; amplitude is usable directly.

---

## 2. System architecture

Two chips, one link, four stages.

The Arduino Nano 33 IoT contains **two** processors:

- a **SAMD21** (the main MCU, talks to the PC over native USB), and
- a **u-blox NINA-W102** module housing an **ESP32-D0WDQ6** (dual-core, 2 MB flash, DIO
  flash mode) that does the WiFi.

They are wired together by an internal UART. In normal Arduino use the ESP32 runs u-blox
firmware and is driven via the SPI-based `WiFiNINA` library. Here, the ESP32 is reflashed
with custom firmware that captures raw CSI and streams it over that internal UART; the
SAMD21 becomes a transparent bridge to the PC.

### Data flow

1. **ESP32** associates with a 2.4 GHz AP, pings the gateway to generate a steady stream
   of received packets, computes CSI on each, and prints `CSI,<rssi>,<ch>,<len>,<b0>,…`
   lines over its UART0.
2. **SAMD21** forwards those lines from the NINA UART (`SerialNina`) to USB, and also reads
   the PIR pin, injecting `PIR,1` / `PIR,0` lines **only at CSI line boundaries** so they
   never corrupt a CSI record.
3. **Laptop** (`csi_ingest_ae.py`) reads the serial stream, separates the two line types,
   classifies activity (only while the PIR reports presence), and:
   - posts `presence` and `activity` results to the **oneM2M CSE** (control plane), and
   - streams the raw amplitude vector over a **websocket** (data plane, local only).
4. **Browser** (`ruview.html`) polls the CSE for the latest classification and reads the
   websocket for the live waterfall.

### Control plane vs data plane

This split is deliberate and is the core architectural decision. oneM2M is designed for
discrete, low-rate telemetry, not a high-rate signal firehose. So:

- **Control plane (via oneM2M):** presence events and activity classifications — low-rate,
  discrete, the "meaningful decisions." Any other application can consume them from the CSE.
- **Data plane (local websocket):** the raw CSI amplitudes for the waterfall — high-rate,
  bulky, never routed through the service layer.

The dashboard consumes each from the appropriate place, which is why the raw CSI never
appears in the CSE.

### The cascade

The PIR is an always-on, cheap presence trigger; CSI is the heavier "what are they doing"
sensor. The PIR fires → presence is posted to oneM2M → classification is enabled. The two
are **complementary**: PIR catches motion-based entry instantly and cheaply; CSI then
distinguishes activity *including staying still*, which a PIR alone reports as "absent" the
moment motion stops.

---

## 3. Firmware

### 3.1 ESP32 — `CSI_Extractor.ino`

Key design points:

- **Watchdog-safe callback.** The CSI callback runs inside the WiFi driver task. It does
  the minimum — copies the buffer into a FreeRTOS queue and returns. All printing happens
  in `loop()`, draining the queue. Doing slow `Serial.print`s inside the callback would
  starve the WiFi task and trip the task watchdog (which looks exactly like a boot loop the
  moment real traffic arrives). The queue send is non-blocking: if `loop()` falls behind,
  samples are dropped rather than stalling the radio.
- **Associate + ping.** CSI is only computed on *received* packets. The firmware joins the
  AP and pings the gateway (~10 Hz); each ping reply is a received packet, giving a steady,
  regular CSI stream from one fixed link (the consistent geometry activity sensing needs).
  It does not set a channel manually — the AP dictates it once associated.
- **Config order.** `esp_wifi_set_csi_config` → `esp_wifi_set_csi_rx_cb` → `esp_wifi_set_csi(true)`
  (configure and register the handler before enabling).
- **Output.** Human-readable ASCII (`CSI,rssi,channel,len,bytes…`) at 115200 baud. ASCII is
  the throughput bottleneck at 115200 (~10 lines/s ceiling); binary framing at a higher
  baud is the upgrade path if higher rates are needed.

### 3.2 SAMD21 — `bridge_with_pir.ino`

- Boots the ESP32 into app mode (drive `NINA_GPIO0` high, pulse `NINA_RESETN`).
- Forwards `SerialNina` → USB and USB → `SerialNina`.
- Reads the PIR on `D2` with `INPUT_PULLDOWN` (so an idle/unconnected pin reads a stable
  LOW instead of floating and generating phantom triggers).
- **Boundary-safe PIR reporting:** a `PIR,x` line is emitted only immediately after a
  newline is forwarded from the CSI stream, guaranteeing it is always its own clean line
  and never spliced mid-record. (This is the same class of bug that an earlier debug
  heartbeat caused; the boundary rule prevents it.)

> **Critical board fact.** On the Nano 33 IoT, `Serial1` is the external D0/D1 header pins,
> **not** the NINA link. The NINA's UART is `SerialNina` (internally `Serial2` / SERCOM3 on
> pins 29/30). The bridge must use `SerialNina`. Using `Serial1` reads disconnected pins
> and yields only noise.

---

## 4. Flashing the ESP32 (through the SAMD21)

The ESP32 is not directly accessible; the SAMD21 must act as a USB-to-ESP32 bridge.

1. **Put the SAMD21 in passthrough mode.** Upload the stock `SerialNINAPassthrough`
   example (board type: *Arduino Nano 33 IoT*). This wires USB directly to the NINA and
   lets the toolchain reach the ESP32's bootloader.
2. **Flash the ESP32 via the Arduino IDE.** Board: *ESP32 Dev Module*. Settings that
   matter for the NINA-W102:
   - **Flash Size: 2 MB** (not the 4 MB default — the module has 2 MB)
   - **Partition Scheme: Minimal** (the only stock scheme that fits 2 MB)
   - **Flash Mode: DIO** (the NINA flash is wired for DIO, not the QIO default)
   Then **Upload** `CSI_Extractor.ino`. Port stays `/dev/ttyACM0` (you talk through the
   SAMD21).
3. **Return the SAMD21 to bridge duty.** Switch the board type back to *Arduino Nano 33 IoT*
   and upload `bridge_with_pir.ino`.

> **Why let the IDE flash the whole image.** Hand-flashing the four binaries (bootloader
> @0x1000, partitions @0x8000, boot_app0 @0xe000, app @0x10000) with `esptool` caused a
> persistent `invalid magic number 0x0 / Failed to verify partition table` boot loop —
> because a hand-assembled bootloader and partition table can disagree (e.g. flash clock
> divider mismatch). Letting the IDE build and flash the full, internally consistent image
> in one step resolved it. Flashing overwrites the u-blox WiFi firmware, so `WiFiNINA` no
> longer works on the board until that firmware is restored.

The recurring loop for any ESP32 reflash: **passthrough → IDE upload → bridge sketch → run.**

---

## 5. The machine-learning pipeline

### 5.1 Feature extraction (`csi_features.py`)

Both training and live inference import this module so the features can never drift apart.

- **Parsing** (`parse_csi_line`): validates the line, requires `len` to match the value
  count (this drops corrupted lines, PIR lines, boot text, and partial records), and
  returns the amplitude vector for **all** subcarriers (masking is applied later, globally).
- **Windowing** (`make_windows`): slices the stream into 2-second windows with 0.5 s hop
  (75% overlap). Window length is computed from the measured sample rate `fs`, so every
  window covers 2 real seconds regardless of rate drift. Overlap multiplies training data
  but makes adjacent windows near-duplicates — which is why validation must hold out whole
  sessions (§5.3).
- **Features** (`extract_features`): each 2-second window → a fixed-length vector:
  - the window is divided by its own mean amplitude to remove absolute-power (so the model
    can't shortcut on signal strength); power is re-added as exactly one feature;
  - per-subcarrier **mean** (channel shape / posture), **std** (fluctuation), and **motion**
    (mean packet-to-packet change);
  - spectral features of the band-averaged time series (walking has a ~1–2 Hz gait rhythm
    a still person lacks);
  - global scalars (motion mean/std/max, 90th percentile, first-vs-last packet correlation).

### 5.2 Null masking (`build_null_mask`)

Computed **once, globally, across every session** (never per-session, or feature columns
would not align between recordings). A subcarrier is dropped if its mean amplitude is ~0
(a true null) or its variance is ~0 (a constant header artifact). The resulting mask is
saved inside the model bundle so live inference applies the identical mask the model was
trained with. Typical result: 192 raw → ~169 active.

### 5.3 Training and honest validation (`csi_train.py`)

- **Leave-one-session-out cross-validation.** Because overlapping windows are near-copies,
  a random train/test split would leak near-duplicates across the boundary and report a
  falsely high accuracy. Instead, each *recording session* is held out entirely as the test
  set. The reported number is the accuracy on sessions the model never saw.
- **Model:** Random Forest. With ~hundreds of windows and ~500 features, a hand-featured
  Random Forest beats a deep model (which would memorise the room).
- **Label merging:** the `--merge` flag collapses `sitting` + `standing` into `still`,
  producing the 3-class model. This is not hiding the hard case — it removes a question the
  hardware cannot answer (static posture A vs static posture B).
- The script warns if held-out accuracy exceeds 97% (suspicious for single-link CSI) and
  prints the confusion matrix and top features so you can confirm the model learned channel
  structure (per-subcarrier mean/std) rather than the power shortcut.

### 5.4 Data collection (`csi_collect.py`)

Records one labeled session per run to `data/<label>__<n>.npz`. **Record several separate
sessions per label** (3+), stopping and restarting between them — each run is one held-out
unit, and the between-session variation forces the model to learn the activity rather than
one frozen room state. Keep the router and board fixed; vary incidentals (where you sit,
the exact walking path). Static classes (`sitting`, `standing`) must be genuinely still —
watch the live motion readout while recording.

---

## 6. oneM2M integration

### 6.1 CSE

The system uses the **ACME** oneM2M CSE (Python), configured via its guided onboarding:

| Setting | Value |
|---------|-------|
| CSE type | IN-CSE |
| CSE-ID | `id-in` |
| CSE resource name | `cse-in` |
| HTTP port | 8080 |
| Database | in-memory (tree is wiped on restart) |
| **CORS** | **`[http.cors] enable = true`** (required for the `file://` dashboard) |

### 6.2 Resource tree

```
cse-in
└── wifi-sensing            (AE, aei = Cwifisensing)
    ├── presence            (container, mni = 60)   ← PIR edges as contentInstances
    └── activity            (container, mni = 120)  ← classifier results (gated on presence)
```

Presence instances: `{"present": 0|1, "ts": …}`.
Activity instances: `{"activity": "empty|still|walking", "confidence": …, "ts": …}`.

### 6.3 Client (`onem2m.py`)

A minimal oneM2M HTTP client using standard headers (`X-M2M-Origin`, `X-M2M-RI`,
`X-M2M-RVI`, and `ty` in the create `Content-Type`). No vendor library, so the same code
works against other oneM2M CSEs (e.g. Eclipse OM2M) by changing the base URL. It registers
the AE with a provisional `C…` originator, treats "already registered" (403) and conflict
(409) as success by recovering the existing AE-ID, and creates the containers idempotently.

### 6.4 Runtime (`csi_ingest_ae.py`)

Owns the serial port and produces both outputs from one reader: classification to the CSE
(gated on PIR presence), and the raw amplitude vector over a websocket. When presence goes
false, the rolling window is cleared so stale data cannot leak across the gate. Also
publishes sample rate and packet count on the websocket for the dashboard's liveness
readouts.

---

## 7. Running the full system

```bash
# Terminal 1 — CSE (with CORS enabled in acme.ini)
cd cse && acmecse

# Terminal 2 — the runtime
python csi_ingest_ae.py --port /dev/ttyACM0 --model model_still.joblib --cse http://localhost:8080

# Browser — dashboard
#   open ruview.html   (or, to avoid file:// quirks: python -m http.server 9000)
#   ACME's own resource tree is viewable at http://localhost:8080
```

Acceptance test:

1. Empty room → presence reads a steady **ABSENT** (no churn).
2. Walk into the PIR field → **PRESENT** within ~1 s; activity panel shows **WALKING**.
3. Stand still in view → **STILL** (the class a PIR alone cannot report).
4. Leave → **ABSENT**; classification gates off.
5. Header shows live Hz and climbing packet count; raw waterfall scrolls.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Garbage / boxes on serial | Baud mismatch between ESP32 and SAMD21 `SerialNina` | Match both to 115200 |
| Nothing on serial at all | Bridge reading `Serial1` instead of `SerialNina` | Use `SerialNina` |
| `invalid magic number 0x0` boot loop | Hand-flashed image inconsistent / wrong flash mode | Flash via IDE (2 MB / Minimal / DIO) |
| Dashboard shows "No CSE" / CORS error | CORS disabled | `[http.cors] enable = true`, restart CSE |
| Dashboard stuck on old class | Query returned oldest instances | Fetch the `la` (latest) resource |
| `Errno 98 address already in use` | Zombie `csi_ingest_ae.py` holding port 8765 | `pkill -f csi_ingest_ae.py`, wait 3 s, retry |
| `403 Originator has already registered` | AE still in the (in-memory) CSE | Handled in code; or restart the CSE |
| PIR flips true/false with no one there | Floating input pin | `INPUT_PULLDOWN`; tune HC-SR501 time-delay pot; allow 60 s warm-up |

---

## 9. Results and findings

Leave-one-session-out validation:

- **empty / still / walking: 98.4%.** Confusion matrix is near-diagonal; the few errors are
  `still`↔`walking` at the exact motion threshold (a still person who shifted).
- **empty / sitting / standing / walking: 86.2%.** The residual error is overwhelmingly
  `sitting`↔`standing`.

**Interpretation.** Walking is separated from stillness near-perfectly because motion
inflates per-subcarrier variance in a way stillness never does — close to linearly
separable. The two *static* postures cannot be reliably separated on one antenna, because
they differ only in where a motionless body sits in the multipath, and a single link has no
angular resolution to distinguish that. The 12-point jump from merging them localises the
hardware's resolution limit precisely: **this rig senses motion vs stillness, not static
posture.** The feature importances confirm the model learned per-subcarrier channel
structure, not the absolute-power shortcut.

---

## 10. Limitations and future work

- **No joint-level pose.** Requires multi-antenna spatial diversity (the reference
  DensePose-from-WiFi work used 3×3 antennas) plus wider bandwidth for range resolution.
  Feasible extension: add 2–3 bare ESP32 nodes for multi-link coarse localisation.
- **Environment- and session-specific.** Retraining is needed if the geometry changes. A
  next-day, test-only run against the trained model characterises how well it generalises
  and is the recommended validation before deployment claims.
- **Software gating, not power gating.** The ESP32 radio runs continuously.
- **In-memory CSE.** The oneM2M tree does not persist across CSE restarts; switch the ACME
  database backend for persistence.
- **Vital signs excluded by design.** Heart rate is not recoverable on a single 8-bit link;
  respiration is redundant with `still` and unreliable under motion.
- **Does not identify individuals, by design.** The system characterises presence and
  activity (entered / still / moving), not identity. This is intentional: for a
  privacy-preserving monitor, the inability to identify *who* entered is a feature, and it
  is the design boundary — not a shortfall of the method.
