"""User config for devbox itself: $DEVBOX_CONFIG or ~/.config/devbox/config.toml.

Only the devbox server reads it. A missing or broken file means defaults,
never an error, so a typo can't stop message delivery.
"""
import os
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10 on a remote target, which never reads the file
    tomllib = None

_cache = {}


def path():
    return Path(os.environ.get("DEVBOX_CONFIG") or Path.home() / ".config/devbox/config.toml")


def load():
    """The parsed config, re-read when the file changes."""
    if tomllib is None:
        return {}
    config = path()
    try:
        stamp = config.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache.get("key") != (config, stamp):
        try:
            data = tomllib.loads(config.read_text())
        except (OSError, ValueError) as exc:
            print("devbox: ignoring %s: %s" % (config, exc), file=sys.stderr)
            data = {}
        _cache.update(key=(config, stamp), data=data)
    return _cache["data"]


def section(name):
    value = load().get(name)
    return value if isinstance(value, dict) else {}
