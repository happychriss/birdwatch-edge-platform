#include "batch_store.h"
#include "config.h"

#include "esp_log.h"
#include "esp_spiffs.h"
#include "esp_timer.h"

#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *TAG = "batch_store";

// Record layout: [uint32 meta_len][meta json][jpeg bytes].  One file per
// record, named with a zero-padded sequence so readdir order is chronological.
#define REC_MAGIC_LEN  4
#define SEQ_KEY_FILE   BW_BATCH_MOUNT "/seq"

static bool     s_ready   = false;
static uint32_t s_dropped = 0;


esp_err_t bw_batch_init(void)
{
    if (s_ready) return ESP_OK;

    esp_vfs_spiffs_conf_t conf = {
        .base_path              = BW_BATCH_MOUNT,
        .partition_label        = BW_BATCH_PARTITION,
        .max_files              = 4,
        .format_if_mount_failed = true,
    };
    esp_err_t err = esp_vfs_spiffs_register(&conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPIFFS mount failed: %s", esp_err_to_name(err));
        return err;
    }
    size_t total = 0, used = 0;
    if (esp_spiffs_info(BW_BATCH_PARTITION, &total, &used) == ESP_OK)
        ESP_LOGI(TAG, "store mounted: %u/%u bytes used", (unsigned)used, (unsigned)total);
    s_ready = true;
    return ESP_OK;
}


// Iterate the record files, oldest first.  Returns the count and, optionally,
// the total bytes and the name of the oldest record.
static int scan(size_t *bytes_out, char *oldest_out, size_t oldest_cap)
{
    if (bytes_out) *bytes_out = 0;
    if (oldest_out && oldest_cap) oldest_out[0] = '\0';

    DIR *d = opendir(BW_BATCH_MOUNT);
    if (!d) return 0;

    int n = 0;
    char oldest[64] = {0};
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        const char *dot = strrchr(e->d_name, '.');
        if (!dot || strcmp(dot, ".rec") != 0) continue;
        n++;
        if (bytes_out) {
            char path[128];
            snprintf(path, sizeof(path), "%s/%.48s", BW_BATCH_MOUNT, e->d_name);
            struct stat st;
            if (stat(path, &st) == 0) *bytes_out += (size_t)st.st_size;
        }
        // Names are zero-padded sequence numbers, so lexical order == age order.
        if (oldest[0] == '\0' || strcmp(e->d_name, oldest) < 0)
            snprintf(oldest, sizeof(oldest), "%.48s", e->d_name);
    }
    closedir(d);
    if (oldest_out && oldest_cap) snprintf(oldest_out, oldest_cap, "%.48s", oldest);
    return n;
}


int    bw_batch_count(void)   { return s_ready ? scan(NULL, NULL, 0) : 0; }
uint32_t bw_batch_dropped(void) { return s_dropped; }

size_t bw_batch_bytes(void)
{
    if (!s_ready) return 0;
    size_t b = 0;
    scan(&b, NULL, 0);
    return b;
}


static uint32_t next_seq(void)
{
    uint32_t seq = 0;
    FILE *f = fopen(SEQ_KEY_FILE, "rb");
    if (f) { fread(&seq, sizeof(seq), 1, f); fclose(f); }
    seq++;
    f = fopen(SEQ_KEY_FILE, "wb");
    if (f) { fwrite(&seq, sizeof(seq), 1, f); fclose(f); }
    return seq;
}


static void drop_oldest(void)
{
    char oldest[64];
    if (scan(NULL, oldest, sizeof(oldest)) == 0 || oldest[0] == '\0') return;
    char path[128];
    snprintf(path, sizeof(path), "%s/%.48s", BW_BATCH_MOUNT, oldest);
    if (unlink(path) == 0) {
        s_dropped++;
        ESP_LOGW(TAG, "store full — dropped oldest record %s (total dropped %lu)",
                 oldest, (unsigned long)s_dropped);
    }
}


esp_err_t bw_batch_append(const char *meta_json, const uint8_t *jpg, size_t jpg_len)
{
    if (!s_ready) return ESP_ERR_INVALID_STATE;
    if (!meta_json) meta_json = "{}";
    size_t meta_len = strlen(meta_json);
    size_t need = REC_MAGIC_LEN + meta_len + jpg_len;

    // Free space oldest-first until this record fits the byte budget.
    size_t used = bw_batch_bytes();
    int guard = 64;
    while (used + need > BW_BATCH_MAX_BYTES && guard-- > 0) {
        int before = bw_batch_count();
        drop_oldest();
        if (bw_batch_count() >= before) break;   // nothing removable; stop
        used = bw_batch_bytes();
    }

    char path[128];
    snprintf(path, sizeof(path), "%s/%08lu.rec", BW_BATCH_MOUNT,
             (unsigned long)next_seq());
    FILE *f = fopen(path, "wb");
    if (!f) {
        ESP_LOGE(TAG, "cannot open %s for write", path);
        return ESP_FAIL;
    }
    uint32_t ml = (uint32_t)meta_len;
    bool ok = fwrite(&ml, sizeof(ml), 1, f) == 1
           && (meta_len == 0 || fwrite(meta_json, 1, meta_len, f) == meta_len)
           && (jpg_len  == 0 || fwrite(jpg, 1, jpg_len, f) == jpg_len);
    fclose(f);
    if (!ok) {
        unlink(path);
        ESP_LOGE(TAG, "short write for %s — record discarded", path);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "stored %s (%u bytes meta + %u bytes jpeg)",
             path, (unsigned)meta_len, (unsigned)jpg_len);
    return ESP_OK;
}


int bw_batch_flush(esp_err_t (*send)(const char *, const uint8_t *, size_t),
                   int max_records, uint32_t deadline_ms)
{
    if (!s_ready || !send) return 0;
    int64_t t0 = esp_timer_get_time();
    int sent = 0;

    for (int i = 0; i < max_records; i++) {
        if ((esp_timer_get_time() - t0) / 1000 > (int64_t)deadline_ms) {
            ESP_LOGW(TAG, "flush deadline reached — %d record(s) still pending",
                     bw_batch_count());
            break;
        }
        char oldest[64];
        if (scan(NULL, oldest, sizeof(oldest)) == 0 || oldest[0] == '\0') break;

        char path[128];
        snprintf(path, sizeof(path), "%s/%.48s", BW_BATCH_MOUNT, oldest);
        FILE *f = fopen(path, "rb");
        if (!f) { unlink(path); continue; }

        struct stat st;
        if (stat(path, &st) != 0 || st.st_size < REC_MAGIC_LEN) {
            fclose(f); unlink(path); continue;
        }
        uint32_t ml = 0;
        if (fread(&ml, sizeof(ml), 1, f) != 1 || ml > (uint32_t)st.st_size) {
            fclose(f); unlink(path);
            ESP_LOGW(TAG, "corrupt record %s — discarded", oldest);
            continue;
        }
        size_t jpg_len = (size_t)st.st_size - REC_MAGIC_LEN - ml;
        char   *meta = malloc(ml + 1);
        uint8_t *jpg = jpg_len ? malloc(jpg_len) : NULL;
        if (!meta || (jpg_len && !jpg)) {
            free(meta); free(jpg); fclose(f);
            ESP_LOGE(TAG, "out of memory reading %s", oldest);
            break;                              // leave it for the next cycle
        }
        bool ok = (ml == 0 || fread(meta, 1, ml, f) == ml)
               && (jpg_len == 0 || fread(jpg, 1, jpg_len, f) == jpg_len);
        meta[ml] = '\0';
        fclose(f);

        if (ok && send(meta, jpg, jpg_len) == ESP_OK) {
            unlink(path);                       // delete only once accepted
            sent++;
        } else {
            free(meta); free(jpg);
            ESP_LOGW(TAG, "send failed for %s — kept for next cycle", oldest);
            break;                              // stop; likely the link is down
        }
        free(meta); free(jpg);
    }
    if (sent) ESP_LOGI(TAG, "flushed %d record(s), %d pending", sent, bw_batch_count());
    return sent;
}
