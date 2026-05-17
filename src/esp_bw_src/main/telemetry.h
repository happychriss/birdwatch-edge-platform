#pragma once
#include <stdint.h>
// ─── Per-capture telemetry buffer ─────────────────────────────────────────────
// Accumulates key-value pairs into a cJSON object that is serialised once for
// the multipart upload.  Adding a new observable is one call at the point where
// the value is computed; nothing else needs to change.
//
// Typical usage:
//   bw_tele_reset();             // start of a new capture cycle (called by bw_cc_assess)
//   bw_tele_f("ratio", ratio);   // anywhere a value is available
//   ...
//   const char *json = bw_tele_json();   // serialise; valid until next reset()
//
// All functions are no-ops when called without a prior reset() or after alloc
// failure — the JSON will simply be missing those keys.

#include <stdbool.h>

void        bw_tele_reset(void);
void        bw_tele_i(const char *key, long   val);
void        bw_tele_f(const char *key, double val);
void        bw_tele_s(const char *key, const char *val);
void        bw_tele_b(const char *key, bool   val);
// Add an array of uint8 values (e.g. tile_means) as a JSON number array.
void        bw_tele_arr_u8(const char *key, const uint8_t *vals, int len);

// Returns a serialised JSON string (no pretty-print).
// Valid until the next bw_tele_reset() call.  Returns "{}" on alloc failure.
const char *bw_tele_json(void);
