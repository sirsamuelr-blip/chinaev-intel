# ChinaEV Intel

![CI](https://github.com/sirsamuelr-blip/chinaev-intel/actions/workflows/ci.yml/badge.svg)

Automated competitive intelligence pipeline that monitors Chinese EV industry sources, extracts structured data about software features and competitive moves, and delivers weekly intelligence briefs to subscribers.

## Quick Start

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in API keys
3. Install Python 3.12
4. Install dependencies: `cd backend && pip install -r requirements.txt`
5. Install Playwright browsers: `python -m playwright install chromium`
6. Install pre-commit hooks: `pre-commit install && pre-commit install --hook-type commit-msg`
7. Run tests: `cd backend && pytest`

See [CONTRIBUTING.md](CONTRIBUTING.md) for full development workflow.

## Architecture

- **Backend** (Python 3.12): custom scrapers, Claude API extraction pipeline, Firestore operations
- **Frontend** (React 18 + Vite + Tailwind): admin dashboard + subscriber dashboard
- **Database**: Firebase Firestore (denormalized document model)
- **Infrastructure**: Vercel (frontend), Railway (workers), HK/SG VPS (scrapers)

## License

[MIT](LICENSE)
