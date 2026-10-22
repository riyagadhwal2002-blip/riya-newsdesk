"""Logging with automatic redaction of secrets (API keys, tokens)."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TOKEN_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+"),
    re.compile(r"(key=)[^&\s\"']+"),
    re.compile(r"(bot)\d+:[A-Za-z0-9_-]+"),
]


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        for pat in _TOKEN_PATTERNS:
            text = pat.sub(lambda m: m.group(1) + "***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(log_dir: Path, secrets: list[str], verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("newsdesk")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    redact = RedactFilter(secrets)

    file_handler = RotatingFileHandler(Path(log_dir) / "newsdesk.log", maxBytes=2_000_000, backupCount=5)
    stream = logging.StreamHandler()
    for h in (file_handler, stream):
        h.setFormatter(fmt)
        h.addFilter(redact)
        logger.addHandler(h)
    return logger


log = logging.getLogger("newsdesk")
