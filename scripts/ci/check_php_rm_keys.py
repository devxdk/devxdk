#!/usr/bin/env python3
"""Detect PHP release-manager roster changes; never automatically trust a key."""
import pathlib
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from devxdk_manifest import config, fetch  # noqa: E402

URL = "https://www.php.net/gpg-keys.php"


class RosterParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sections = {}
        self.heading = None
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self.current = None
            self.heading = []

    def handle_endtag(self, tag):
        if tag == "h3" and self.heading is not None:
            heading = "".join(self.heading).strip()
            self.heading = None
            match = re.fullmatch(r"PHP (\d+\.\d+)", heading)
            if match:
                self.current = match[1]
                if self.current in self.sections:
                    raise ValueError(f"duplicate PHP {self.current} section")
                self.sections[self.current] = []

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        elif self.current is not None:
            self.sections[self.current].append(data)


def check(html, cfg):
    parser = RosterParser()
    parser.feed(html)
    pinned = set(cfg.pins["php_keys"]["fingerprints"])
    errors = []
    for line in cfg.component("php").lines.values():
        if line.retired:
            continue
        section = "".join(parser.sections.get(line.id, []))
        blocks = re.findall(r"(?m)^\s*pub\s+[^\n]*\n\s*([^\n]+)", section)
        fingerprints = [re.sub(r"\s+", "", value).upper() for value in blocks]
        if not fingerprints or any(not re.fullmatch(r"[0-9A-F]{40}", value) for value in fingerprints):
            errors.append(f"PHP {line.id}: missing or malformed release-manager section")
            continue
        missing = set(fingerprints) - pinned
        if missing:
            errors.append(f"PHP {line.id}: unpinned release managers: {', '.join(sorted(missing))}")
    return errors


def main():
    try:
        errors = check(fetch.Fetcher().get_text(URL), config.load())
    except (fetch.FetchError, config.ConfigError, ValueError, KeyError) as exc:
        errors = [str(exc)]
    for error in errors:
        print(f"PHP roster: {error}", file=sys.stderr)
    if not errors:
        print("PHP roster: all active lines covered by pinned release managers")
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
