void setup()
{
  // opens the high-speed USB CDC connection directly to PC
  Serial.begin(921600);

  // opens the internal hardware serial link connected directly to the u-blox module
  Serial1.begin(921600);

  // takes control of internal power and state pins
  pinMode(NINA_RESETN, OUTPUT);
  pinMode(NINA_GPIO0, OUTPUT);

  // boots the esp32 into standard app mode, out of bootloader state
  digitalWrite(NINA_GPIO0, HIGH);
  digitalWrite(NINA_RESETN, HIGH);
}

void loop()
{
  // pulls high frequency CSI packets out of the ESP32 and stream to the USB port
  while (Serial1.available())
  {
    Serial.write(Serial1.read());
  }

  // route configuration commands from PC down to ESP32 if needed
  while (Serial.available())
  {
    Serial1.write(Serial.read());
  }
}