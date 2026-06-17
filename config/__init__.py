"""Repose OS shared configuration loader.

All operator-editable values live in repose/config/repose_config.yaml.
Nothing is hardcoded in /src/.

Usage:
    from repose.config import repose_config
    tg = repose_config["telegram"]["bot_token_secret_id"]
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.environ.get(
    "REPOSE_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent / "config" / "repose_config.yaml"),
)

_repose_config: dict = {}


def _load_config() -> dict:
    """Load and cache the shared Repose config."""
    global _repose_config
    if _repose_config:
        return _repose_config

    config_path = Path(_CONFIG_PATH)
    if not config_path.exists():
        logger.warning("Config file not found at %s, using defaults", config_path)
        _repose_config = {}
        return _repose_config

    import yaml

    with open(config_path) as fh:
        _repose_config = yaml.safe_load(fh) or {}

    logger.info("Loaded config from %s", config_path)
    return _repose_config


def reload_config() -> dict:
    """Force reload config from disk."""
    global _repose_config
    _repose_config = {}
    return _load_config()


# Module-level lazy singleton — accessed as repose.config.repose_config
class _ConfigProxy:
    """Lazy-loading config proxy that supports dict-like access."""

    def __getitem__(self, key):
        return _load_config()[key]

    def get(self, key, default=None):
        return _load_config().get(key, default)

    def __contains__(self, key):
        return key in _load_config()

    def __iter__(self):
        return iter(_load_config())

    def __len__(self):
        return len(_load_config())

    def keys(self):
        return _load_config().keys()

    def values(self):
        return _load_config().values()

    def items(self):
        return _load_config().items()


repose_config = _ConfigProxy()
