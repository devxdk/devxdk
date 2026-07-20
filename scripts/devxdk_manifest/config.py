"""Loader and validator for ``config/tracked-versions.toml``.

This is the single source of truth: the scrapers, builders, and validators all
read the component/line/platform tree and the build pins from here. The loader
fails closed on any structural problem so a malformed config can never reach a
signing job.

Standard library only (``tomllib``, Python 3.11+).
"""

from __future__ import annotations

import pathlib
import tomllib
from dataclasses import dataclass, field

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "config" / "tracked-versions.toml"

VALID_KINDS = {"runtime", "service"}
VALID_TYPES = {"scrape", "adopt", "build"}
VALID_CHANNELS = {"stable", "lts", "prerelease"}
# Platform keys the manifest schema resolves (goos/goarch, darwin fallbacks, and
# composer's "any"). Kept in sync with internal/manifest PlatformKeys.
VALID_PLATFORM_KEYS = {
    "windows/amd64",
    "linux/amd64",
    "darwin/amd64",
    "darwin/arm64",
    "darwin/universal",
    "any",
}

SCHEMA_VERSION = 1


class ConfigError(ValueError):
    """Raised on any structural problem in tracked-versions.toml."""


@dataclass(frozen=True)
class Platform:
    key: str          # "windows/amd64", "any", ...
    type: str         # scrape | adopt | build
    provider: str     # stable provider identity
    epoch: int        # provider epoch (default 1)

    @property
    def managed(self) -> bool:
        """A built or adopted asset (ordering-ledger governed), not scraped."""
        return self.type in ("adopt", "build")

    @property
    def ordering_kind(self) -> str:
        """The ledger's kind for this platform: build -> built, adopt -> adopted."""
        return "built" if self.type == "build" else "adopted"


@dataclass(frozen=True)
class Line:
    id: str
    channel: str
    retain_per_line: int
    retired: bool
    platforms: dict          # key -> Platform


@dataclass
class Component:
    name: str
    kind: str
    lines: dict = field(default_factory=dict)   # id -> Line


@dataclass
class Config:
    schema: int
    components: dict          # name -> Component
    pins: dict                # raw pins table
    path: pathlib.Path

    # -- iteration helpers -------------------------------------------------

    def component(self, name: str) -> Component:
        try:
            return self.components[name]
        except KeyError:
            raise ConfigError(f"unknown component {name!r}") from None

    def platform_keys(self):
        """Yield (component, line_id, platform_key, Platform) for every entry."""
        for cname, comp in self.components.items():
            for lid, line in comp.lines.items():
                for pkey, plat in line.platforms.items():
                    yield cname, lid, pkey, plat

    def scrape_keys(self):
        """Every scrape-type (component, line, platform) — the scrape guard's set."""
        for cname, lid, pkey, plat in self.platform_keys():
            if plat.type == "scrape":
                yield cname, lid, pkey, plat

    def managed_keys(self):
        """Every build/adopt (component, line, platform) — the leg/ledger set."""
        for cname, lid, pkey, plat in self.platform_keys():
            if plat.managed:
                yield cname, lid, pkey, plat

    def line(self, component: str, line_id: str) -> Line:
        comp = self.component(component)
        try:
            return comp.lines[line_id]
        except KeyError:
            raise ConfigError(f"unknown line {component}/{line_id}") from None

    def find_platform(self, component: str, line_id: str, pkey: str) -> Platform:
        line = self.line(component, line_id)
        try:
            return line.platforms[pkey]
        except KeyError:
            raise ConfigError(
                f"unknown platform {component}/{line_id}/{pkey}"
            ) from None


def load(path=None) -> Config:
    """Parse and validate tracked-versions.toml. Raises ConfigError on any fault."""
    p = pathlib.Path(path) if path else DEFAULT_PATH
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config not found: {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: invalid TOML: {e}") from None
    return _parse(raw, p)


def _parse(raw: dict, path: pathlib.Path) -> Config:
    schema = raw.get("schema")
    if schema != SCHEMA_VERSION:
        raise ConfigError(f"schema = {schema!r}, want {SCHEMA_VERSION}")

    comps_raw = raw.get("components")
    if not isinstance(comps_raw, dict) or not comps_raw:
        raise ConfigError("no [components] table")

    components = {}
    for cname, cval in comps_raw.items():
        components[cname] = _parse_component(cname, cval)

    pins = raw.get("pins", {})
    if not isinstance(pins, dict):
        raise ConfigError("[pins] must be a table")

    return Config(schema=schema, components=components, pins=pins, path=path)


def _parse_component(name: str, val) -> Component:
    if not isinstance(val, dict):
        raise ConfigError(f"component {name!r} must be a table")
    kind = val.get("kind")
    if kind not in VALID_KINDS:
        raise ConfigError(f"component {name!r}: kind = {kind!r}, want one of {sorted(VALID_KINDS)}")
    lines_raw = val.get("lines")
    if not isinstance(lines_raw, dict) or not lines_raw:
        raise ConfigError(f"component {name!r}: no lines")
    comp = Component(name=name, kind=kind)
    for lid, lval in lines_raw.items():
        comp.lines[str(lid)] = _parse_line(name, str(lid), lval)
    return comp


def _parse_line(cname: str, lid: str, val) -> Line:
    if not isinstance(val, dict):
        raise ConfigError(f"line {cname}/{lid} must be a table")
    channel = val.get("channel")
    if channel not in VALID_CHANNELS:
        raise ConfigError(f"line {cname}/{lid}: channel = {channel!r}, want one of {sorted(VALID_CHANNELS)}")
    retain = val.get("retain_per_line")
    if not isinstance(retain, int) or isinstance(retain, bool) or retain < 1:
        raise ConfigError(f"line {cname}/{lid}: retain_per_line must be a positive integer")
    retired = val.get("retired", False)
    if not isinstance(retired, bool):
        raise ConfigError(f"line {cname}/{lid}: retired must be a boolean")
    plats_raw = val.get("platforms")
    if not isinstance(plats_raw, dict) or not plats_raw:
        raise ConfigError(f"line {cname}/{lid}: no platforms")
    platforms = {}
    for pkey, pval in plats_raw.items():
        platforms[pkey] = _parse_platform(cname, lid, pkey, pval)
    return Line(id=lid, channel=channel, retain_per_line=retain, retired=retired, platforms=platforms)


def _parse_platform(cname: str, lid: str, pkey: str, val) -> Platform:
    if pkey not in VALID_PLATFORM_KEYS:
        raise ConfigError(f"platform {cname}/{lid}/{pkey!r}: not a valid platform key")
    if not isinstance(val, dict):
        raise ConfigError(f"platform {cname}/{lid}/{pkey}: must be an inline table")
    ptype = val.get("type")
    if ptype not in VALID_TYPES:
        raise ConfigError(f"platform {cname}/{lid}/{pkey}: type = {ptype!r}, want one of {sorted(VALID_TYPES)}")
    provider = val.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ConfigError(f"platform {cname}/{lid}/{pkey}: provider must be a non-empty string")
    epoch = val.get("epoch", 1)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise ConfigError(f"platform {cname}/{lid}/{pkey}: epoch must be a positive integer")
    unknown = set(val) - {"type", "provider", "epoch"}
    if unknown:
        raise ConfigError(f"platform {cname}/{lid}/{pkey}: unknown keys {sorted(unknown)}")
    return Platform(key=pkey, type=ptype, provider=provider, epoch=epoch)
