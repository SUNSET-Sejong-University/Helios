/*
  CSI_Extractor.ino  -- watchdog-safe CSI capture for the u-blox NINA (ESP32-D0WDQ6)
  inside an Arduino Nano 33 IoT.
*/

#include "WiFi.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"
#include <string.h>

const char *SSID = "DLive_1C7A";
const char *PASSWORD = "E0012D1C79";

#define PING_INTERVAL_MS 100  // 10 pings/sec; ~ceiling for ASCII CSI over the 115200 link
#define CSI_MAX_LEN 512       // generous cap for one CSI buffer

typedef struct
{
  int8_t rssi;
  uint8_t channel;
  uint16_t len;
  int8_t buf[CSI_MAX_LEN];
} csi_sample_t;

static QueueHandle_t csi_queue = NULL;

// runs the wifi task context; copy and return, no serial output
void csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
  if (!info || !info->buf || csi_queue == NULL) return;

  csi_sample_t s;
  s.rssi = info->rx_ctrl.rssi;
  s.channel = info->rx_ctrl.channel;
  s.len = (info->len > CSI_MAX_LEN) ? CSI_MAX_LEN : info->len;
  memcpy(s.buf, info->buf, s.len);

  // non blocking: if loop() cant keep up, drop the sample rather than stall the WiFi task
  xQueueSend(csi_queue, &s, 0);
}

static void start_csi()
{
  wifi_csi_config_t csi_config = {
    .lltf_en = true,
    .htltf_en = true,
    .stbc_htltf2_en = true,
    .ltf_merge_en = true,
    .channel_filter_en = true,
    .manu_scale = false,
    .shift = false,
  };
  esp_wifi_set_csi_config(&csi_config);     // first config
  esp_wifi_set_csi_rx_cb(&csi_rx_cb, NULL); // then register callback
  esp_wifi_set_csi(true);                   // then enable
}

// pings the gateway forever so we keep receiving packets and thus CSI
static void start_ping_to_gateway()
{
  IPAddress gw = WiFi.gatewayIP();

  ip_addr_t target;
  IP_ADDR4(&target, gw[0], gw[1], gw[2], gw[3]);

  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  cfg.target_addr = target;
  cfg.count = ESP_PING_COUNT_INFINITE;
  cfg.interval_ms = PING_INTERVAL_MS;

  esp_ping_callbacks_t cbs = {};
  esp_ping_handle_t ping;
  if (esp_ping_new_session(&cfg, &cbs, &ping) == ESP_OK)
  {
    esp_ping_start(ping);
  }
}

void setup()
{
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("CSI : BOOTING");

  csi_queue = xQueueCreate(20, sizeof(csi_sample_t));

  WiFi.mode(WIFI_STA);
  WiFi.begin(SSID, PASSWORD);
  Serial.print("CSI : CONNECTING");
  while (WiFi.status() != WL_CONNECTED)
  {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("CSI : CONNECTED ch="); Serial.print(WiFi.channel());
  Serial.print(" ip="); Serial.print(WiFi.localIP());
  Serial.print(" gw="); Serial.print(WiFi.gatewayIP());

  start_csi();
  start_ping_to_gateway();

  Serial.println("CSI : Streaming -> CSI,<rssi>,<channel>,<len>,<b0>,<b1>,...");
}

void loop()
{
  csi_sample_t s;
  // Drain everything the WiFi task queued. All printing happens HERE.
  while (xQueueReceive(csi_queue, &s, 0) == pdTRUE)
  {
    Serial.print("CSI,");
    Serial.print(s.rssi); Serial.print(",");
    Serial.print(s.channel); Serial.print(",");
    Serial.print(s.len);
    for (uint16_t i = 0; i < s.len; i++)
    {
      Serial.print(",");
      Serial.print(s.buf[i]);
    }
    Serial.println();
  }
}