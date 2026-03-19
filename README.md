# Immich Cleaner

Automatically classify and describe photos in your [Immich](https://immich.app) library using a local AI vision model via [Ollama](https://ollama.com). One API call per photo gives you two results:

1. **Classification** (TRASH / REVIEW / KEEP) — sorted into Immich albums for easy bulk review
2. **Description** (one sentence) — written to Immich metadata, making photos searchable by content

No cloud APIs, no data leaves your network.

## How it works

```
immich-cleaner
  ├─ Immich API     — fetches thumbnails, creates albums, writes descriptions
  ├─ Ollama API     — classifies photos via vision model
  └─ SQLite         — tracks processed photos (resumes on restart)
```

**Categories:**
- **TRASH** → album "To Delete": screenshots, accidental shots, receipts, QR codes, completely blurry/black frames
- **REVIEW** → album "To Review": slightly blurry, too dark/bright, questionable quality
- **KEEP** → description only (default when the model is unsure)

On first run, it processes your entire library. Then it watches for new photos and classifies them automatically.

## Requirements

- [Immich](https://immich.app) instance (v1.106+)
- [Ollama](https://ollama.com) with a vision model pulled
- Docker & Docker Compose

## Quick start

**1. Pull a vision model in Ollama:**

```bash
ollama pull qwen3-vl:4b
```

Other options: `llava`, `moondream`, `minicpm-v` — any model that accepts images.

**2. Clone and configure:**

```bash
git clone https://github.com/yourname/immich-cleaner.git
cd immich-cleaner
cp .env.example .env
```

Edit `.env` and set your `IMMICH_API_KEY` (required). If Immich runs on a different machine, update `IMMICH_API_URL` too.

**3. Run:**

```bash
docker compose up -d --build
docker compose logs -f
```

### Getting your Immich API key

1. Open Immich web UI
2. Go to **User Settings** → **API Keys**
3. Create a new key, copy it to `.env`

## Configuration

All settings via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `IMMICH_API_KEY` | *(required)* | Immich API key |
| `IMMICH_API_URL` | `http://host.docker.internal:2283` | Immich server URL (as seen from inside Docker) |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama API URL |
| `OLLAMA_MODEL` | `qwen3-vl:4b` | Ollama vision model name |
| `CONCURRENCY` | `1` | Parallel requests to Ollama |
| `MODE` | `continuous` | `once` — process all and exit; `continuous` — process all, then watch for new |
| `CHECK_INTERVAL` | `3600` | Seconds between scans for new photos (continuous mode) |
| `MAX_ASSETS` | `0` | Limit photos to process (0 = unlimited, useful for testing) |
| `WRITE_DESCRIPTIONS` | `false` | Write AI descriptions to Immich photo metadata (see warning below) |
| `ALBUM_TRASH` | `To Delete` | Album name for TRASH photos |
| `ALBUM_REVIEW` | `To Review` | Album name for REVIEW photos |

> **Note:** `host.docker.internal` refers to your host machine from inside the Docker container. This works out of the box on Docker Desktop (macOS/Windows). On Linux, you may need to use your host's LAN IP instead (e.g. `http://192.168.1.100:2283`).

### About descriptions

When `WRITE_DESCRIPTIONS=true`, the AI generates a one-sentence description for each photo and writes it to the Immich description field. This makes photos searchable by content (e.g. "dog on the beach").

**This is disabled by default** because it overwrites any existing descriptions you may have added manually. Enable it only if you're comfortable with this.

## Performance

Speed depends on your hardware and the chosen model. Example on Apple M4 with `qwen3-vl:4b`:

| Concurrency | Approx. speed | Time for 25K photos |
|-------------|---------------|---------------------|
| 1 | ~375/hr | ~67 hours |
| 3 | ~1,100/hr | ~23 hours |

For initial bulk processing, run with higher concurrency:

```bash
CONCURRENCY=3 MODE=once docker compose up -d --build
```

## Network setup

**Immich and Ollama on the same machine (most common):**
Default settings work on Docker Desktop. Both use `host.docker.internal` to reach host services.

**Ollama on a different machine:**
Set `OLLAMA_URL=http://192.168.1.x:11434` in `.env`.

**Immich on a different machine:**
Set `IMMICH_API_URL=http://192.168.1.x:2283` in `.env`.

**Custom hostname for Immich:**
If you use a hostname like `photos.local` that doesn't resolve inside Docker, add `extra_hosts` to `docker-compose.yml`:
```yaml
extra_hosts:
  - "photos.local:192.168.1.100"
```

**Linux Docker (no Docker Desktop):**
`host.docker.internal` may not work. Use your host's LAN IP in both `IMMICH_API_URL` and `OLLAMA_URL`.

## Checking progress

```bash
# Live logs
docker compose logs -f

# Stats
docker exec immich_cleaner python -c "
import sqlite3
db = sqlite3.connect('/data/cleaner.db')
for row in db.execute('SELECT category, COUNT(*) FROM processed GROUP BY category'):
    print(f'{row[0]}: {row[1]}')
print(f'Total: {db.execute(\"SELECT COUNT(*) FROM processed\").fetchone()[0]}')
"
```

In Immich UI:
- **Albums** → "To Delete" / "To Review" — review and bulk-delete junk
- **Search** — photo descriptions are searchable via metadata search

## How classification works

The vision model receives each photo's thumbnail and returns a category + description using these rules:

- **TRASH**: screenshots, screen recordings, accidental shots (pocket/floor/ceiling), utility images (receipts, QR codes, meter readings, tickets, documents), technical failures (completely blurry, black, or white frames)
- **REVIEW**: slightly blurry but recognizable, too dark/bright but still visible, low quality but possibly meaningful
- **KEEP**: people, pets, places, events, food, nature, selfies — any intentional photo. When unsure, defaults to KEEP

You can customize these rules by overriding `SYSTEM_PROMPT` and `USER_PROMPT` environment variables.

## Troubleshooting

**Container exits immediately:**
Check logs with `docker compose logs`. Most likely `IMMICH_API_KEY` is not set or Ollama is not reachable.

**"Ollama unavailable" in logs:**
Make sure Ollama is running and the URL is correct. From inside Docker, `localhost` means the container itself — use `host.docker.internal` or your host's LAN IP.

**Slow processing:**
Increase `CONCURRENCY` (2-4 is usually safe). Speed depends primarily on your GPU/CPU and the model size.

**Want to reprocess all photos:**
Delete the SQLite database: `docker volume rm immich-cleaner_cleaner-data`, then restart.

## Inspired by

- [immich-analyze](https://github.com/timasoft/immich-analyze) — AI-powered image description generator for Immich (Rust)

## License

[MIT](LICENSE)
