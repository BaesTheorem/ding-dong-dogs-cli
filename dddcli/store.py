"""On-disk state: profile, per-restaurant cart and session, cached operation hashes.

Everything lives under ~/.config/ddd (override with DDD_CONFIG_DIR). Files are written
0600 because the profile holds a name, email and phone. Card details never land here.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("DDD_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".config" / "ddd"


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or config_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._cache: dict[str, dict] = {}

    def load(self, name: str) -> dict:
        if name not in self._cache:
            path = self.root / name
            try:
                self._cache[name] = json.loads(path.read_text())
            except (OSError, ValueError):
                self._cache[name] = {}
        return self._cache[name]

    def save(self, name: str, data: dict | None = None) -> None:
        if data is not None:
            self._cache[name] = data
        path = self.root / name
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{name}.")
        with os.fdopen(fd, "w") as f:
            json.dump(self._cache[name], f, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # ---- typed helpers -------------------------------------------------------

    @property
    def profile(self) -> dict:
        return self.load("config.json").setdefault("profile", {})

    def save_profile(self, profile: dict) -> None:
        cfg = self.load("config.json")
        cfg["profile"] = profile
        self.save("config.json")

    def restaurant(self, slug: str) -> dict:
        return self.load("state.json").setdefault("restaurants", {}).setdefault(slug, {})

    def save_state(self) -> None:
        self.save("state.json")

    @property
    def hashes(self) -> dict:
        return self.load("state.json").get("hashes") or {}

    def save_hashes(self, hashes: dict, meta: dict) -> None:
        st = self.load("state.json")
        st["hashes"] = hashes
        st["hashes_meta"] = meta
        self.save("state.json")
