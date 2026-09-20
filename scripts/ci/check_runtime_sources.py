#!/usr/bin/env python3
"""Detect frozen cache-source indexes and emit a reviewable coupled pin update."""
import argparse
import difflib
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from devxdk_manifest import config, fetch, resolvers


class SourceTexts:
    def __init__(self, client):
        self.client, self.values = client, {}

    def get_text(self, url, headers=None):
        if url not in self.values:
            self.values[url] = self.client.get_text(url, headers)
        return self.values[url]


def check(cfg, client):
    errors, replacements = [], {}
    texts = SourceTexts(client)
    for name, repo in (('redis', 'redis/redis-hashes'), ('valkey', 'valkey-io/valkey-hashes')):
        pinned = cfg.pins[name + '_hashes']['ref']
        head = client.get_json(f'https://api.github.com/repos/{repo}/commits/HEAD', resolvers._gh_headers()).get('sha')
        if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head):
            raise ValueError(f'{repo}: invalid upstream commit identity')
        for lid, line in cfg.component(name).lines.items():
            if line.retired or line.historical_only:
                continue
            prior = resolvers.hashes_newest(texts, repo, pinned, name, lid)
            latest = resolvers.hashes_newest(texts, repo, head, name, lid)
            if (prior['source_version'], prior['source_sha256']) != (latest['source_version'], latest['source_sha256']):
                errors.append(f"{name} {lid}: pinned index offers {prior['source_version']}; upstream offers {latest['source_version']} (review source index {head})")
                replacements[pinned] = head
    return errors, replacements


def proposal(root, replacements):
    changes = []
    for relative in ('config/tracked-versions.toml', '.github/versions-inventory.toml'):
        original = (root / relative).read_text(encoding='utf-8')
        updated = original
        for prior, current in replacements.items():
            updated = updated.replace(prior, current)
        changes.extend(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
                                          fromfile='a/' + relative, tofile='b/' + relative))
    return ''.join(changes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proposal-file', default='runtime-pin-update.diff')
    args = parser.parse_args()
    cfg = config.load()
    try:
        errors, replacements = check(cfg, fetch.Fetcher())
        patch = proposal(cfg.path.parent.parent, replacements)
    except (fetch.FetchError, resolvers.ResolveError, ValueError, KeyError) as exc:
        print(f'Runtime source currency: {exc}', file=sys.stderr)
        return 1
    pathlib.Path(args.proposal_file).write_text(patch, encoding='utf-8', newline='\n')
    for error in errors:
        print(error, file=sys.stderr)
    if patch and os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write('Source index update requires review before rebuilding.\n\n```diff\n' + patch + '```\n')
    if not errors:
        print('Runtime source currency: configured cache families match upstream')
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
