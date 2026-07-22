"""Per-upstream scrape adapters.

Each adapter exposes ``build(fetcher, ...) -> component-manifest dict`` and takes
a fetch.Fetcher (or a fake with the same surface) so it is testable offline. The
node and go adapters reproduce gen-manifest.py byte-for-byte; composer folds the
Phase-0 hand-seed into real scraping; the remaining sources land with the rest of
the Phase-2 scrape-breadth work.
"""

from . import composer, go, mariadb, node

__all__ = ["node", "go", "composer", "mariadb"]
