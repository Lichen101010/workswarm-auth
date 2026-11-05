"""Identifiers accepted by both transports; no server or storage dependencies."""
import re

SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
