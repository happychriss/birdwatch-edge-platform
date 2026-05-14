#pragma once
// ─── Remote log forwarder ─────────────────────────────────────────────────────
// When BW_REMOTE_LOG is 1, installs a vprintf hook that enqueues every
// ESP_LOGx line and a background task POSTs batches to BW_LOG_URL (port 8000).
// UART output is preserved.  Call after WiFi is up; deinit before disconnect.

#include "config.h"

#if BW_REMOTE_LOG
void bw_remote_log_init(void);
void bw_remote_log_deinit(void);
#else
static inline void bw_remote_log_init(void)   {}
static inline void bw_remote_log_deinit(void) {}
#endif
