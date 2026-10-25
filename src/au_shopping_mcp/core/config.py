"""Environment config + JSON logging (one line per event, Loki-friendly)."""
import json
import logging
import os
import sys
import time
from typing import Any

from au_shopping_mcp.core.models import FetchTier

# Which adapters this instance serves: src/au_shopping_mcp/adapters/<name>.py.
# SITES is a comma-separated list or "all"; one process then serves every listed
# site. SITE (one adapter) is the older single-site form and still works when
# SITES is unset. Tool names are prefixed with the adapter name either way.
SITE = os.environ.get("SITE", "bunnings")
SITES = os.environ.get("SITES", "")

PORT = int(os.environ.get("PORT", "8347"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")

# Cache, cookie jars and the persistent Chromium profile live here. Mount it
# in the container so Cloudflare clearance survives restarts.
DATA_DIR = os.environ.get("DATA_DIR", os.path.expanduser("~/.local/state/au-shopping-mcp"))
CACHE_DIR = os.path.join(DATA_DIR, "cache")
BROWSER_PROFILE_DIR = os.path.join(DATA_DIR, "browser-profile")

# Ceiling on the fetch ladder. 1 = httpx only (no Chromium), 2 = headless
# Playwright, 3 = Playwright with persistent profile + stealth tweaks.
MAX_TIER = FetchTier(int(os.environ.get("MAX_TIER", "3")))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Site-specific knobs are read by the adapter itself, e.g. BUNNINGS_STORE_ID.


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)  # stderr: stdio transport owns stdout
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(LOG_LEVEL)


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    """log_event(log, "fetch", domain=..., tier=..., status=...) -> one structured line."""
    logger.log(level, msg, extra={"extra_fields": fields})
