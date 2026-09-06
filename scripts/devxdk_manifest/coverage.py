"""Operation-scoped coverage over the accepted state, optionally projected forward."""
import datetime
import pathlib

from . import config, merge, pending, publication as pub, receipts, schema, strictjson


def check(operation, root, project_pending=False):
    root = pathlib.Path(root)
    cfg = config.load(root / 'config/tracked-versions.toml')
    ledger = merge.LedgerState.load(root / 'state/asset-revisions.json')
    scraped = merge.ScrapeState.load(root / 'state/scrape-versions.json')
    if project_pending:
        records = [pending.PendingRecord.from_dict(strictjson.load(p)) for p in sorted((root / 'pending').glob('*.json'))]
        pending.apply_pending_records(cfg, ledger, scraped, records, datetime.date.today().isoformat())
    errors, documents = [], {}
    for target in operation['targets']:
        component, version, platform = target['component'], target['version'], target['platform']
        if component not in documents:
            original = schema.load(root / f'{component}.json')
            documents[component] = merge.recompose(component, original['display_name'], original['kind'], cfg, scraped, ledger)
        release = next((r for r in documents[component]['releases'] if r['version'] == version), None)
        platforms = (release or {}).get('platforms', {})
        missing = set(target['coverage_platforms']) - platforms.keys()
        if missing:
            errors.append(f"{component} {version}: missing platforms {', '.join(sorted(missing))}")
        rec = ledger.get(component, version, platform)
        if (rec is None or rec.revoked or rec.status != 'active' or rec.provider != target['provider']
                or rec.epoch != target['epoch'] or rec.kind != target['ordering_kind']
                or rec.source_version != target['source_version']
                or (rec.kind == 'built' and rec.key != str(target['revision']))):
            errors.append(f"{component} {version} {platform}: planned source/provider/epoch/revision not accepted")
            continue
        receipt = receipts.load(root, target)
        expected = target.get('accepted_asset')
        if receipt is not None:
            expected = {'url': pub.download_url(receipt['meta']), **{k: receipt['meta'][k] for k in ('sha256', 'size_bytes')}}
        if expected is None or any(getattr(rec, k) != expected[k] for k in ('url','sha256','size_bytes')):
            errors.append(f"{component} {version} {platform}: accepted bytes do not match the operation")
    return sorted(set(errors)), documents
