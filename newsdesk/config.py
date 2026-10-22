"""Loads config.yaml plus secrets from the environment / a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SECRET_KEYS = (
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "IG_ACCESS_TOKEN",
    "TELEGRAM_BOT_TOKEN",
)


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=value per line). Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    raw: dict
    root: Path
    data_dir: Path
    public_dir: Path
    public_base_url: str
    env: dict = field(default_factory=dict)

    # convenience accessors -------------------------------------------------
    def section(self, name: str) -> dict:
        return self.raw.get(name, {}) or {}

    @property
    def mode(self) -> str:
        # Env var wins so you can flip modes without editing files.
        return (os.environ.get("NEWSDESK_MODE") or self.section("publishing").get("mode", "dry_run")).lower()

    def secret(self, key: str) -> str | None:
        value = os.environ.get(key)
        return value or None

    def secret_values(self) -> list[str]:
        return [v for k in SECRET_KEYS if (v := os.environ.get(k))]


def load_settings(config_path: str | Path | None = None, env_path: str | Path | None = None) -> Settings:
    root = PROJECT_ROOT
    load_dotenv(Path(env_path) if env_path else root / ".env")
    cfg_path = Path(config_path or os.environ.get("NEWSDESK_CONFIG") or root / "config.yaml")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    data_dir = Path(os.environ.get("NEWSDESK_DATA_DIR") or root / "data")
    public_dir = Path(os.environ.get("PUBLIC_DIR") or data_dir / "public")
    for d in (data_dir, public_dir, data_dir / "posts", data_dir / "logs"):
        d.mkdir(parents=True, exist_ok=True)

    return Settings(
        raw=raw,
        root=root,
        data_dir=data_dir,
        public_dir=public_dir,
        public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/"),
    )
