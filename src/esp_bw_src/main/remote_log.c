#include "remote_log.h"

#if BW_REMOTE_LOG

#include <stdio.h>
#include <string.h>
#include <stdarg.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "cJSON.h"

#define LOG_QUEUE_DEPTH  64
#define LOG_LINE_MAX     192
#define LOG_BATCH_MAX    16
#define LOG_FLUSH_MS     500

static QueueHandle_t  s_queue      = NULL;
static TaskHandle_t   s_task       = NULL;
static volatile bool  s_running    = false;
static vprintf_like_t s_prev_vprintf = NULL;

typedef struct { char text[LOG_LINE_MAX]; } log_line_t;

static int remote_vprintf(const char *fmt, va_list args)
{
    // Format first (va_copy before any consumer).
    if (s_queue && s_running) {
        log_line_t line;
        va_list args2;
        va_copy(args2, args);
        vsnprintf(line.text, sizeof(line.text), fmt, args2);
        va_end(args2);
        // Strip trailing newline — server adds its own.
        int n = strlen(line.text);
        while (n > 0 && (line.text[n-1] == '\n' || line.text[n-1] == '\r'))
            line.text[--n] = '\0';
        xQueueSendToBack(s_queue, &line, 0);  // drop if full
    }
    return s_prev_vprintf(fmt, args);
}

static void remote_log_task(void *arg)
{
    log_line_t line;
    while (s_running || uxQueueMessagesWaiting(s_queue)) {
        // Wait for the first line of a batch.
        if (xQueueReceive(s_queue, &line, pdMS_TO_TICKS(LOG_FLUSH_MS)) != pdTRUE)
            continue;

        // Build a JSON array — drain up to LOG_BATCH_MAX more items.
        cJSON *arr = cJSON_CreateArray();
        cJSON_AddItemToArray(arr, cJSON_CreateString(line.text));
        for (int i = 1; i < LOG_BATCH_MAX; i++) {
            if (xQueueReceive(s_queue, &line, 0) != pdTRUE) break;
            cJSON_AddItemToArray(arr, cJSON_CreateString(line.text));
        }

        char *body = cJSON_PrintUnformatted(arr);
        cJSON_Delete(arr);
        if (!body) continue;

        esp_http_client_config_t cfg = {
            .url            = BW_LOG_URL,
            .method         = HTTP_METHOD_POST,
            .timeout_ms     = 3000,
            .skip_cert_common_name_check = true,
        };
        esp_http_client_handle_t client = esp_http_client_init(&cfg);
        esp_http_client_set_header(client, "Content-Type", "application/json");
        esp_http_client_set_post_field(client, body, strlen(body));
        esp_http_client_perform(client);
        esp_http_client_cleanup(client);
        free(body);
    }
    vTaskDelete(NULL);
}

void bw_remote_log_init(void)
{
    s_queue   = xQueueCreate(LOG_QUEUE_DEPTH, sizeof(log_line_t));
    s_running = true;
    xTaskCreate(remote_log_task, "remote_log", 4096, NULL, 3, &s_task);
    s_prev_vprintf = esp_log_set_vprintf(remote_vprintf);
}

void bw_remote_log_deinit(void)
{
    if (s_prev_vprintf) {
        esp_log_set_vprintf(s_prev_vprintf);
        s_prev_vprintf = NULL;
    }
    s_running = false;
    // Give the task time to flush remaining queue items.
    vTaskDelay(pdMS_TO_TICKS(LOG_FLUSH_MS * 2 + 500));
    if (s_queue) {
        vQueueDelete(s_queue);
        s_queue = NULL;
    }
}

#endif // BW_REMOTE_LOG
