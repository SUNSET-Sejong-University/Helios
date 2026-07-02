void setup()
{
  Serial.begin(115200);          // native USB to PC
  while (!Serial) { ; }          // wait for the monitor to actually open (native USB)
  Serial.println("SAMD21 bridge up");

  SerialNina.begin(115200);      // the UART actually wired to the NINA/ESP32

  pinMode(NINA_GPIO0, OUTPUT);
  pinMode(NINA_RESETN, OUTPUT);

  // clean reset into app mode: GPIO0 high, then pulse RESET low -> high
  digitalWrite(NINA_GPIO0, HIGH);
  digitalWrite(NINA_RESETN, LOW);
  delay(100);
  digitalWrite(NINA_RESETN, HIGH);
  //Serial.println("ESP32 reset released");
}

void loop()
{
  while (SerialNina.available()) Serial.write(SerialNina.read());
  while (Serial.available())     SerialNina.write(Serial.read());
}