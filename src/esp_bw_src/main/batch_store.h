#pragma once
// ─── Batched-record store for suppressed PIR events ─────────────────────────
//
// A suppressed event skips WiFi entirely, which is where the battery goes.  But
// suppression that simply discards the frame is irreversible and invisible: the
// device powers off, and a wrongly-dropped bird leaves no trace.  That is what
// would force a timid threshold.
//
// So a suppressed event writes a record here — a JPEG plus the decision inputs
// — and the next cycle that raises WiFi flushes them to the server, where they
// appear as `batched` rows for review.  A wrong suppression becomes a measured
// fact instead of a guess, which is what makes an aggressive threshold safe.
//
// Backed by the `storage` SPIFFS partition.  Bounded by TOTAL BYTES rather than
// record count, because records vary in size (an SVGA thumbnail from the
// suppress path, a full SXGA frame from a burst rejection).  At capacity the
// OLDEST record is dropped and counted.

#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Mount the SPIFFS partition.  Safe to call once per boot; returns ESP_OK if
// the store is usable.  On failure the caller should upload normally rather
// than suppress — never suppress into a store that cannot record.
esp_err_t bw_batch_init(void);

// Append one record.  meta_json is stored verbatim alongside the image bytes.
esp_err_t bw_batch_append(const char *meta_json, const uint8_t *jpg, size_t jpg_len);

// Number of records currently pending.
int bw_batch_count(void);

// Total bytes currently used by pending records.
size_t bw_batch_bytes(void);

// How many records have been dropped for capacity since the last flush — worth
// reporting, because a rising count means the store is undersized or WiFi has
// been failing for a long time.
uint32_t bw_batch_dropped(void);

// Send pending records oldest-first.  `send` returns ESP_OK when the server has
// accepted the record, and only then is it deleted — a failed send leaves the
// record in place for the next cycle.  Stops after max_records or when
// deadline_ms of wall time has elapsed, so a large backlog cannot push the
// cycle past the watchdog.  Returns the number successfully sent.
int bw_batch_flush(esp_err_t (*send)(const char *meta_json,
                                     const uint8_t *jpg, size_t jpg_len),
                   int max_records, uint32_t deadline_ms);
