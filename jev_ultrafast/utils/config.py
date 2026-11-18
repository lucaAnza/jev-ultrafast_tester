"""Small server-side configuration helpers."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_environment(path=None):
    """Fill missing credentials from the project .env, regardless of cwd."""
    environment_file = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not environment_file.exists():
        return environment_file
    for line in environment_file.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = value
    return environment_file
