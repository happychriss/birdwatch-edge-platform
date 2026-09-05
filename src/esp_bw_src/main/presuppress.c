#include "presuppress.h"
#include "presuppress_table.h"
#include "config.h"
#include "telemetry.h"

#include "esp_log.h"
#include "nvs.h"

#include <math.h>
#include <string.h>

static const char *TAG = "presuppress";

#define NVS_NS        "bw_meta"
#define KEY_LAST_PIR  "ps_last"    // u32, UTC epoch of the previous PIR event
#define KEY_BURST     "ps_burst"   // u8,  consecutive triggers <60 s apart
#define KEY_THRESHOLD "ps_thr"     // u8,  overrides BW_PS_DEFAULT_THRESHOLD

// A gap this large means "no previous event on record" — the first boot, or a
// stale/erased NVS.  Well past the top gap band, so it lands in the safest cell.
#define GAP_UNKNOWN   1000000


float bw_presup_solar_elev(time_t now_utc)
{
    struct tm gm;
    gmtime_r(&now_utc, &gm);
    float doy  = (float)(gm.tm_yday + 1);
    float hour = gm.tm_hour + gm.tm_min / 60.0f + gm.tm_sec / 3600.0f;

    float g = 2.0f * (float)M_PI / 365.0f * (doy - 1.0f + (hour - 12.0f) / 24.0f);
    float eqtime = 229.18f * (0.000075f
                 + 0.001868f * cosf(g)     - 0.032077f * sinf(g)
                 - 0.014615f * cosf(2*g)   - 0.040849f * sinf(2*g));
    float decl = 0.006918f - 0.399912f * cosf(g)   + 0.070257f * sinf(g)
               - 0.006758f * cosf(2*g)   + 0.000907f * sinf(2*g)
               - 0.002697f * cosf(3*g)   + 0.00148f  * sinf(3*g);

    float ha  = ((hour * 60.0f + eqtime + 4.0f * BW_GEO_LON_DEG) / 4.0f - 180.0f)
                * (float)M_PI / 180.0f;
    float lat = BW_GEO_LAT_DEG * (float)M_PI / 180.0f;
    float cz  = sinf(lat) * sinf(decl) + cosf(lat) * cosf(decl) * cosf(ha);
    if (cz >  1.0f) cz =  1.0f;
    if (cz < -1.0f) cz = -1.0f;
    return 90.0f - acosf(cz) * 180.0f / (float)M_PI;
}


static int band_f(float v, const float *edges, int n)
{
    int i = 0;
    for (int k = 0; k < n; k++)
        if (v >= edges[k]) i++;
    return i;
}


static uint8_t table_score(float elev, int32_t gap_s, uint8_t burst)
{
    int e = band_f(elev, BW_PS_ELEV_EDGES,
                   sizeof(BW_PS_ELEV_EDGES) / sizeof(BW_PS_ELEV_EDGES[0]));
    int g = band_f((float)gap_s, BW_PS_GAP_EDGES,
                   sizeof(BW_PS_GAP_EDGES) / sizeof(BW_PS_GAP_EDGES[0]));
    int b = burst > BW_PS_BURST_MAX ? BW_PS_BURST_MAX : burst;
    int idx = (e * BW_PS_N_GAP + g) * BW_PS_N_BURST + b;
    if (idx < 0 || idx >= (int)(sizeof(BW_PS_TABLE) / sizeof(BW_PS_TABLE[0]))) {
        ESP_LOGW(TAG, "cell index %d out of range — treating as PROCEED", idx);
        return 255;
    }
    return BW_PS_TABLE[idx];
}


// Read the previous PIR epoch and burst counter.  Missing keys are not an
// error: on a fresh flash there simply is no history yet.
static void state_load(uint32_t *last_pir, uint8_t *burst, uint8_t *threshold)
{
    *last_pir  = 0;
    *burst     = 0;
    *threshold = BW_PS_DEFAULT_THRESHOLD;

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK)
        return;
    nvs_get_u32(h, KEY_LAST_PIR, last_pir);
    nvs_get_u8 (h, KEY_BURST,    burst);
    nvs_get_u8 (h, KEY_THRESHOLD, threshold);
    nvs_close(h);
}


esp_err_t bw_presup_decide(time_t now_utc, bool is_pir, bw_presup_t *out)
{
    if (!out) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof(*out));
    out->quiet_gap_s = -1;

    uint32_t last_pir;
    state_load(&last_pir, &out->burst_pos, &out->threshold);

    // No clock, no decision — never suppress on a guess.
    if (now_utc <= 0) {
        out->suppress = false;
        out->score    = 255;
        strncpy(out->why, "NO_TIME", sizeof(out->why) - 1);
        ESP_LOGW(TAG, "RTC unreadable — proceeding unconditionally");
        return ESP_OK;
    }

    out->solar_elev = bw_presup_solar_elev(now_utc);

    // RTC frames are the proof-of-life and flush the batch store; never drop one.
    if (!is_pir) {
        out->suppress = false;
        out->score    = 255;
        strncpy(out->why, "RTC", sizeof(out->why) - 1);
        return ESP_OK;
    }

    int32_t gap = (last_pir == 0) ? GAP_UNKNOWN : (int32_t)(now_utc - (time_t)last_pir);
    if (gap < 0) gap = GAP_UNKNOWN;      // clock moved backwards (NTP sync); be safe
    out->quiet_gap_s = gap;
    // burst_pos was loaded as the PREVIOUS event's position; advance it here so
    // the score sees this event's own position, matching how the rule was fitted.
    out->burst_pos = (gap < 60) ? (uint8_t)(out->burst_pos + 1) : 0;

    out->score    = table_score(out->solar_elev, gap, out->burst_pos);
    out->suppress = (out->score < out->threshold);
    strncpy(out->why, out->suppress ? "SCORE" : "PROCEED", sizeof(out->why) - 1);

    ESP_LOGI(TAG, "elev=%.1f gap=%lds burst=%u score=%u thr=%u → %s",
             out->solar_elev, (long)gap, out->burst_pos, out->score,
             out->threshold, out->suppress ? "SUPPRESS" : "PROCEED");
    return ESP_OK;
}


void bw_presup_commit(time_t now_utc, bool is_pir)
{
    // Only PIR events advance the quiet-gap / burst state: the rule was fitted
    // over the PIR event sequence, so letting RTC wakeups reset the gap would
    // change what the table means.
    if (!is_pir || now_utc <= 0) return;

    uint32_t last_pir; uint8_t burst, thr;
    state_load(&last_pir, &burst, &thr);
    int32_t gap = (last_pir == 0) ? GAP_UNKNOWN : (int32_t)(now_utc - (time_t)last_pir);
    if (gap < 0) gap = GAP_UNKNOWN;
    uint8_t next_burst = (gap < 60) ? (uint8_t)(burst + 1) : 0;
    if (next_burst > 250) next_burst = 250;    // saturate; only <=BURST_MAX matters

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "state commit: nvs_open failed");
        return;
    }
    nvs_set_u32(h, KEY_LAST_PIR, (uint32_t)now_utc);
    nvs_set_u8 (h, KEY_BURST,    next_burst);
    nvs_commit(h);
    nvs_close(h);
}


void bw_presup_telemetry(const bw_presup_t *d)
{
    if (!d) return;
    bw_tele_f("solar_elev", (double)d->solar_elev);
    bw_tele_i("quiet_gap",  (long)d->quiet_gap_s);
    bw_tele_i("burst_pos",  (long)d->burst_pos);
    bw_tele_i("ps_score",   (long)d->score);
    bw_tele_i("ps_thr",     (long)d->threshold);
    bw_tele_s("why",        d->why);
}
