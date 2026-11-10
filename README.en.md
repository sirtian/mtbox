# mtbox

A lightweight **M-Team subscription downloader** for NAS + Docker + qBittorrent.
Server-rendered, single process, SQLite. No frontend build step, no TMDB dependency.

[中文](README.md) | **English**

> **Disclaimer**: This project is not affiliated with or endorsed by M-Team, Douban,
> qBittorrent or Zspace. Run it only with accounts and networks you are allowed to use,
> and follow the tracker's rules and your local laws. It does not provide, host, or
> distribute any media. See [compliance](docs/compliance.md).

## Features

- **Subscriptions** — match M-Team releases by title/year/season, detect episode
  ranges, and fill missing episodes.
- **Candidates first** — generate scored candidates with rule details; download only
  after confirmation by default.
- **Optional auto-grab** — per-subscription, selecting the best seed combination
  (multiple torrents when needed to cover all aired episodes).
- **Enhanced search** — M-Team categories/subcategories, server-side sorting,
  discount filters, pagination, and keyword-free browsing; results show both the
  human-readable title and the release file name.
- **Douban charts** — browse trending TV/movie/variety with poster cards,
  filter, and prefill subscriptions. Public pages only; no Douban account or
  cookie. Posters load through a narrow same-origin proxy because Douban's CDN
  rejects direct requests from a LAN origin.
- **Download and organize** — push to qBittorrent, poll progress, then hardlink and
  rename into your library for the media server to scrape.
- **Ratio guard** — switch to freeleech-only or pause grabbing when account ratio or
  disk space gets low.
- **Seeding cleanup** — remove seeds after configured seeding time/ratio, prioritizing
  cleanup when disk is tight.
- **Notifications** — WeCom, ServerChan, SMTP, plus a daily digest.
- **Runtime settings** — scoring, budgets, guards, naming templates and notifications
  are editable in the UI without a restart.
- **Safe by design** — M-Team Open API with `x-api-key` only; no cookies, no login
  endpoints; secrets are never echoed, logged, or embedded in download URLs.

## How it works

```
Browser ──► FastAPI (Jinja2 SSR) ──► SQLite
                 │
                 ├─► M-Team Open API  (search / token, rate limited)
                 ├─► qBittorrent      (add / poll / delete)
                 └─► hardlink+rename  ──► media library

APScheduler (in-process) ──► the same service layer
```

## Quick start

### Docker Compose

```bash
cd mtbox/deploy/nas
cp .env.example .env
# edit MT_API_KEY, QB_URL, QB_PASSWORD, ADMIN_PASSWORD and host paths
docker compose up -d
```

Open `http://NAS_IP:18080/` and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
Build the image yourself if you do not have one:

```bash
docker build -t mtbox:0.6.9 .
```

### Local

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
mtbox check-config
mtbox serve
```

Required settings: `MT_API_KEY`, `QB_URL`/`QB_USERNAME`/`QB_PASSWORD`,
`ADMIN_PASSWORD`, the three container paths, and `PATH_MAP_JSON`. See the
[configuration reference](docs/configuration.md) (Chinese).

## Constraints

- **qBittorrent and mtbox must mount the same download directory at the same path**
  (default `/downloads`). Use `PATH_MAP_JSON` when the paths differ.
- **Hardlinks require one filesystem.** If `DOWNLOAD_DIR` and `LIBRARY_DIR` are on
  different pools, organization falls back to copying.
- **LAN only, one worker.** Authentication is a single admin password; do not expose
  the app directly to the internet. Rate limiting and scheduler locks are in-process.

## Documentation

Most documents are written in Chinese:

- [Configuration](docs/configuration.md)
- [Usage](docs/usage.md)
- [Docker deployment](docs/deployment/docker.md)
- [Backup and restore](docs/operations/backup-restore.md)
- [Troubleshooting](docs/operations/troubleshooting.md)
- [Architecture](docs/architecture.md)
- [Compliance](docs/compliance.md)
- [Development](docs/development/setup.md)
- [Design documents](docs/README.md)

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described
in [SECURITY.md](SECURITY.md) — do not open a public issue.

## License

[Apache License 2.0](LICENSE). Copyright 2026 mtbox contributors.

Third-party names and trademarks belong to their respective owners. This repository
contains no third-party media or tracker data.
