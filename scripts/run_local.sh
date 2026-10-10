#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi, sqlalchemy, httpx, jinja2, dotenv, psycopg' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
if [[ ! -f .env ]]; then
  .venv/bin/python - <<'PY'
from pathlib import Path
import secrets
template = Path('.env.example').read_text()
Path('.env').write_text(template.replace('APP_API_KEY=\n', 'APP_API_KEY=' + secrets.token_urlsafe(32) + '\n'))
Path('.env').chmod(0o600)
PY
fi
source_name="$(.venv/bin/python -c 'from stock_daily.config import Settings; print(Settings.from_env().source)')"
if [[ "$source_name" == "demo" ]]; then
  .venv/bin/python -m stock_daily demo
else
  echo "真实数据模式：网页启动后，可在另一个终端运行 .venv/bin/python -m stock_daily run"
fi
.venv/bin/python -m stock_daily worker &
worker_pid=$!
cleanup() { kill "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
echo "本地地址：http://127.0.0.1:8000；定时任务已启动。管理密钥可在 .env 的 APP_API_KEY 查看。"
.venv/bin/python -m stock_daily serve

