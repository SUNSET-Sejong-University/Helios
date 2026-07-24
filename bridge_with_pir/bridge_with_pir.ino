/*
  bridge_with_pir.ino  -- SAMD21 bridge for the Nano 33 IoT.

  Does two jobs:
    1. Forwards the ESP32's CSI stream (SerialNina) up to the PC (Serial), and
       forwards anything from the PC back down.
    2. Reads a PIR sensor on a spare pin and reports presence edges to the PC as
       clean "PIR,1" / "PIR,0" lines.

  The critical detail: the PIR line is emitted ONLY immediately after we forward a
  newline from the CSI stream. That guarantees "PIR,x" is always on its own line
  and never spliced into the middle of a "CSI,..." record -- the same splicing bug
  that the old [bridge alive] heartbeat caused. The PC parser ignores any line that
  doesn't start with "CSI,", so the two streams coexist cleanly.

  Wiring (HC-SR501 PIR):
    VCC -> 5V / VUSB      (the module wants 5V power)
    GND -> GND
    OUT -> PIR_PIN below  (its OUT is 3.3V logic -> safe for the SAMD21)
  Do NOT feed any 5V logic line into a SAMD21 GPIO; the board is not 5V tolerant.
*/

#define PIR_PIN 2          // spare digital pin; change to whatever you wired

bool pir_state = false;      // last reported state
bool pir_pending = false;    // an edge is waiting to be emitted at a line boundary
bool at_line_start = true;   // true right after we forwarded a '\n' from the ESP32

void setup() {
  Serial.begin(115200);      // USB CDC to PC
  SerialNina.begin(115200);  // UART to the u-blox ESP32

  pinMode(PIR_PIN, INPUT_PULLDOWN);   // pulldown: floating/idle reads stable LOW (absent);
                                      // a real HC-SR501 drives OUT actively HIGH on motion

  // boot the ESP32 into app mode (unchanged from the working bridge)
  pinMode(NINA_GPIO0, OUTPUT);
  pinMode(NINA_RESETN, OUTPUT);
  digitalWrite(NINA_GPIO0, HIGH);
  digitalWrite(NINA_RESETN, LOW);
  delay(100);
  digitalWrite(NINA_RESETN, HIGH);
}

void loop() {
  // ---- read PIR, debounced by simple edge detection ----
  bool now = digitalRead(PIR_PIN);
  if (now != pir_state) {
    pir_state = now;
    pir_pending = true;      // don't emit yet -- wait for a safe line boundary
  }

  // ---- forward CSI stream up to the PC, tracking line boundaries ----
  while (SerialNina.available()) {
    char c = SerialNina.read();
    Serial.write(c);
    at_line_start = (c == '\n');

    // safe moment to inject our own line: right after a newline
    if (at_line_start && pir_pending) {
      Serial.print("PIR,");
      Serial.println(pir_state ? 1 : 0);
      pir_pending = false;
    }
  }

  // ---- forward PC -> ESP32 (config/commands, if any) ----
  while (Serial.available()) {
    SerialNina.write(Serial.read());
  }
}
