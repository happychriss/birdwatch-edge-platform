#include "telemetry.h"
#include "cJSON.h"

#include <string.h>

static cJSON *s_obj  = NULL;
static char  *s_json = NULL;

void bw_tele_reset(void)
{
    if (s_json) { cJSON_free(s_json); s_json = NULL; }
    if (s_obj)  { cJSON_Delete(s_obj); }
    s_obj = cJSON_CreateObject();
}

void bw_tele_i(const char *key, long val)
{
    if (s_obj) cJSON_AddNumberToObject(s_obj, key, (double)val);
}

void bw_tele_f(const char *key, double val)
{
    if (s_obj) cJSON_AddNumberToObject(s_obj, key, val);
}

void bw_tele_s(const char *key, const char *val)
{
    if (s_obj && val) cJSON_AddStringToObject(s_obj, key, val);
}

void bw_tele_b(const char *key, bool val)
{
    if (!s_obj) return;
    cJSON_AddItemToObject(s_obj, key, val ? cJSON_CreateTrue() : cJSON_CreateFalse());
}

void bw_tele_arr_u8(const char *key, const uint8_t *vals, int len)
{
    if (!s_obj || !vals || len <= 0) return;
    cJSON *arr = cJSON_CreateArray();
    if (!arr) return;
    for (int i = 0; i < len; i++) cJSON_AddItemToArray(arr, cJSON_CreateNumber(vals[i]));
    cJSON_AddItemToObject(s_obj, key, arr);
}

const char *bw_tele_json(void)
{
    if (!s_obj) return "{}";
    if (s_json) { cJSON_free(s_json); }
    s_json = cJSON_PrintUnformatted(s_obj);
    return s_json ? s_json : "{}";
}
