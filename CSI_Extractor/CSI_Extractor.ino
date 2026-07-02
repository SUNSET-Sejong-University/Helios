#include "esp_wifi.h"
#include "esp_wifi_types.h"
#include "esp_system.h"
#include "WiFi.h"

#define TARGET_CHANNEL 6

void csi_raw_callback(void *ctx, wifi_csi_info_t *info)
{
  if (!info || !info->buf) return;
  wifi_pkt_rx_ctrl_t rx_ctrl = info->rx_ctrl;

  // direct stream to internal UART link at max stability speed
  Serial.print("CSI,");
  Serial.print(rx_ctrl.rssi);
  Serial.print(",");
  Serial.print(info->len);
  Serial.print(",");

  int8_t *csi_matrix = (int8_t *)info->buf;
  for (int i = 0; i < info->len; i++)
  {
    Serial.print(csi_matrix[i]);
    if (i < info->len - 1) Serial.print(",");
  }
  Serial.println();
}

void setup()
{
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("BOOT OK - CSI Extractor Starting...");

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(TARGET_CHANNEL, WIFI_SECOND_CHAN_NONE);

  wifi_csi_config_t csi_config = {
    .lltf_en = true,
    .htltf_en = true,
    //.stbc_en = true,
    .ltf_merge_en = true,
    .channel_filter_en = true,
    .manu_scale = 0
  };

  esp_wifi_set_csi(true);
  esp_wifi_set_csi_config(&csi_config);
  esp_wifi_set_csi_rx_cb(csi_raw_callback, NULL);
}

void loop()
{
  static unsigned long last = 0;
  if (millis() - last > 2000)
  {
    last = millis();
    Serial.println("ALIVE");
  }
}