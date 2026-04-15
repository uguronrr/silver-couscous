# Instagram Multi-Target Scraper

This project is a Python scraper pipeline for collecting Instagram profile data with human-paced visit behavior. It supports multi-target interleaving, SQLite storage, optional media download/compression, and CSV/ZIP export.

## Features

- Collect posts and comments from target profiles
- Human-paced visit sessions (visits, breaks, gaps)
- Multi-target interleaving mode (`multistalk`)
- SQLite persistence (`posts`, `comments`, `media_files`)
- Optional media download and compression
- CSV and ZIP export utilities

## Tech stack

- Python 3.11+
- `curl_cffi` for request-layer browser-like TLS behavior
- `playwright` + `playwright-stealth` for browser/warmup flows
- `rich` for CLI tables and status output
- SQLite

## Project structure

- `main.py`: CLI entrypoint
- `multi_stalk.py`: multi-target run controller
- `session_runner.py`: single-target visit scheduler/executor
- `visit.py`: pagination and per-visit scraping flow
- `web_session.py`: Instagram web/API integration
- `storage.py`: database schema and write/read operations
- `media_downloader.py`: media download/compression
- `exporter.py`: ZIP export helper
- `data/session.example.json`: safe cookie template

## Setup (step-by-step)

1. Check Python version:
   ```bash
   python3 --version
   ```

2. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   ```

3. Activate the virtual environment:
   ```bash
   source .venv/bin/activate
   ```

4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

5. Install Playwright Chromium:
   ```bash
   playwright install chromium
   ```

6. Create a runtime session file from template:
   ```bash
   cp data/session.example.json data/session.json
   ```

7. Fill `data/session.json` with valid Instagram cookie values.

## Usage

Show help:

```bash
python main.py -h
```

Run multi-target interleaving:

```bash
python main.py multistalk
```

Run single-target stalk:

```bash
python main.py stalk --target castrolturkiye --total 1004 --brand Castrol
```

Download media for collected posts:

```bash
python main.py download --batch-size 50
```

Export to CSV:

```bash
python main.py export --format csv
```

Export to ZIP:

```bash
python main.py export --format zip
```

Show stats:

```bash
python main.py stats
```

## Runtime outputs

- SQLite DB: `data/opinion_data.db` (generated at runtime)
- CSV export: `data/posts.csv`, `data/comments.csv`
- ZIP export: `data/*.zip`
- Media folder: `data/media/`
