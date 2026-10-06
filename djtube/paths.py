from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
INDEX_PATH = PACKAGE_DIR / "templates" / "index.html"

# Browser-facing prefix. nginx strips this before the request reaches the container.
PUBLIC_PREFIX = "/djtube"
