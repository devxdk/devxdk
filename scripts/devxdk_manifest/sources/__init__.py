"""Per-upstream scrape adapters.

Each adapter exposes ``build(fetcher, ...) -> component-manifest dict`` and takes
a fetch.Fetcher (or a fake with the same surface) so it is testable offline. The
node and go adapters reproduce gen-manifest.py byte-for-byte; the remaining
sources land with the Phase-2 scrape-breadth work.
"""

from . import go, node

__all__ = ["node", "go"]
