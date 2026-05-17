---
name: inspect-frame
description: Inspect a single BirdWatch frame by ID — fetch metadata and tile_means from the database, download the JPEG from the local web server, and analyse pixel statistics vs tile_means to explain any discrepancies.
---

# Inspect Frame by ID

Triggered when the user says things like "check frame #N", "inspect frame N", "show me frame N", "what does frame N look like", etc.

## Why this exists

The `tile_means` stored in the DB come from a **QQVGA (160×120) grayscale** framebuffer captured by the ESP32 for cloud-check analysis. The JPEG uploaded to the server is a separate, higher-resolution color capture. The two can differ in exposure and timing, so visual comparison requires understanding both.

## Steps

### 1. Query the database

```python
import psycopg2, json, os

conn = psycopg2.connect(
    host=os.getenv('DB_HOST', '192.168.1.110'),
    port=int(os.getenv('DB_PORT', 5432)),
    dbname=os.getenv('DB_NAME', 'birdwatch'),
    user=os.getenv('DB_USERNAME', 'birdwatch'),
    password=os.getenv('DB_PASSWORD', '@birdwatch12')
)
cur = conn.cursor()
cur.execute('SELECT id, filename, result, meta FROM bw_frames WHERE id=%s', (FRAME_ID,))
row = cur.fetchone()
frame_id, filename, result, meta = row
conn.close()

global_mean = meta.get('global_mean')
tile_means  = meta.get('tile_means', [])
```

### 2. Download the JPEG

```bash
curl -s "http://localhost:8000/static/<filename>" -o /tmp/frame_<id>.jpg
```

Use the venv that has PIL and numpy:
```
/workspace/src/cloud-check/.venv/bin/python3
```

### 3. Analyse the JPEG

```python
from PIL import Image
import numpy as np

img = Image.open(f'/tmp/frame_{frame_id}.jpg')
arr = np.array(img.convert('L'))
print('JPEG size:', img.size)
print('pixel min/max/mean/std:', arr.min(), arr.max(), round(arr.mean(),1), round(arr.std(),1))
```

### 4. Analyse tile_means

```python
import numpy as np

grid = np.array(tile_means).reshape(12, 16)   # 12 rows × 16 cols (QQVGA 160×120, 10×10 px tiles)
print('tile_means min:', grid.min(), 'max:', grid.max(), 'range:', grid.max()-grid.min())
print('Top row:   ', grid[0])
print('Bottom row:', grid[11])
```

### 5. Interpret

Key facts to report:
- **tile_means** are from the QQVGA grayscale framebuffer, NOT the uploaded JPEG.
- A narrow tile_means range (< 50 DN) means the grayscale frame was nearly uniform — no strong shadows or bright spots in the 160×120 image.
- A wide JPEG pixel range (std > 40) alongside narrow tile_means indicates the two captures had different exposure or scene state.
- The tile grid is 16 columns × 12 rows = 192 tiles. Top-left = tile index 0, bottom-right = tile index 191.
- `global_mean` in meta is the mean of tile_means (integer), used as Stage 0 night-brightness threshold.

## Environment notes

- Python venv with PIL + numpy + psycopg2: `/workspace/src/cloud-check/.venv/bin/python3`
- DB credentials in `/workspace/.env`
- JPEG served at `http://localhost:8000/static/<filename>`
- Downloaded images go to `/tmp/frame_<id>.jpg`
