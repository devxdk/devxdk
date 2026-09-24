#!/usr/bin/env python3
"""Native contract checks for official archives, using the existing scrapers."""
import argparse
import hashlib
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from devxdk_manifest import config, fetch, handoff, strictjson, versions
from publish_legs import download_artifact
from scrape import SOURCES
from smoke_mariadb import smoke as smoke_mariadb

COMPONENTS = ('node', 'go', 'mariadb', 'nginx')


def prepare(directory):
    cfg, client = config.load(), fetch.Fetcher()
    directory.mkdir(parents=True, exist_ok=True)
    for name in COMPONENTS:
        document = SOURCES[name](client, lines=cfg.component(name).lines)
        (directory / (name + '.json')).write_text(json.dumps(document), encoding='utf-8')


def extract(archive, destination):
    destination.mkdir()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for item in source.namelist():
                if not (destination / item).resolve().is_relative_to(destination.resolve()):
                    raise ValueError('archive member escapes destination')
            source.extractall(destination)
    else:
        with tarfile.open(archive, 'r:*') as source:
            source.extractall(destination, filter='data')
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ValueError('archive does not match the single-root installation layout')
    return children[0]


def smoke(name, version, asset, target):
    with tempfile.TemporaryDirectory(prefix='runtime-proof-') as temporary:
        work = pathlib.Path(temporary)
        archive = work / 'download'
        subprocess.run(['curl', '-fsSL', '--retry', '6', '--retry-all-errors', '--retry-max-time', '300',
                        '--connect-timeout', '30', '--max-time', '900', '-o', str(archive), asset['url']], check=True)
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != asset['sha256'] or archive.stat().st_size != asset['size_bytes']:
            raise ValueError('upstream archive bytes differ from verified metadata')
        prefix = extract(archive, work / 'unpacked')
        suffix = '.exe' if target.startswith('windows/') else ''
        env = dict(os.environ)
        if name == 'node':
            binary = prefix / ('node.exe' if suffix else 'bin/node')
            actual = subprocess.check_output([str(binary), '--version'], text=True).strip()
            if actual != 'v' + version:
                raise ValueError(f'Node reports {actual}, expected v{version}')
            npm = prefix / ('node_modules/npm/bin/npm-cli.js' if suffix else 'lib/node_modules/npm/bin/npm-cli.js')
            subprocess.run([str(binary), str(npm), '--version'], check=True, cwd=work)
            subprocess.run([str(binary), '-e', 'if (6 * 7 !== 42) process.exit(1)'], check=True)
        elif name == 'go':
            binary = prefix / 'bin' / ('go' + suffix)
            env.pop('GOROOT', None)
            env.update(GOTOOLCHAIN='local', GOWORK='off', GO111MODULE='off', GOCACHE=str(work / 'go-cache'))
            actual = subprocess.check_output([str(binary), 'version'], env=env, text=True).strip()
            if actual != f'go version go{version} {target}':
                raise ValueError(f'Go reports {actual}, expected {version} {target}')
            (work / 'proof.go').write_text('package main\nimport "fmt"\nfunc main(){fmt.Print(42)}\n')
            result = subprocess.check_output([str(binary), 'run', 'proof.go'], cwd=work, env=env, text=True)
            if result != '42':
                raise ValueError('Go did not compile and execute the proof program')
        elif name == 'mariadb':
            smoke_mariadb(prefix, version)
        else:
            binary = prefix / ('nginx' + suffix)
            result = subprocess.run([str(binary), '-v'], capture_output=True, text=True, check=True)
            match = re.search(r'nginx/([0-9]+\.[0-9]+\.[0-9]+)', result.stdout + result.stderr)
            if match is None or match[1] != version:
                raise ValueError('Nginx does not report the expected version')
            (prefix / 'logs').mkdir(exist_ok=True)
            subprocess.run([str(binary), '-t', '-p', prefix.as_posix() + '/'], cwd=prefix, check=True)


def verify(artifact_id, digest, target, output):
    machine = {'x86_64': 'amd64', 'amd64': 'amd64', 'arm64': 'arm64', 'aarch64': 'arm64'}[platform.machine().lower()]
    system = {'win32': 'windows', 'linux': 'linux', 'darwin': 'darwin'}[sys.platform]
    if target != system + '/' + machine:
        raise ValueError('proof runner does not match the requested native platform')
    cfg, outcomes = config.load(), []
    with tempfile.TemporaryDirectory(prefix='catalog-proof-') as temporary:
        catalog = pathlib.Path(temporary) / 'catalog'
        download_artifact(artifact_id, catalog)
        handoff.verify(catalog, digest)
        for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'ARTIFACT_TOKEN', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN'):
            os.environ.pop(key, None)
        for name in COMPONENTS:
            expected = {lid for lid, line in cfg.component(name).lines.items()
                        if not line.retired and not line.historical_only and target in line.platforms
                        and line.platforms[target].type == 'scrape'}
            document = strictjson.load(catalog / (name + '.json'))
            releases = [r for r in document['releases'] if target in r['platforms']]
            actual = [next((lid for lid in expected if versions.in_family(r['version'], lid)), None) for r in releases]
            if set(actual) != expected or len(actual) != len(expected):
                raise ValueError(f'{name} {target}: catalog does not cover exactly the configured families')
            for release in releases:
                result = {'component': name, 'version': release['version'], 'platform': target}
                try:
                    smoke(name, release['version'], release['platforms'][target], target)
                    result['result'] = 'success'
                    print(f"::notice::{name} {release['version']} {target}: native proof passed", flush=True)
                except Exception as exc:
                    result.update(result='failure', error=str(exc))
                    print(f"::error::{name} {release['version']} {target}: {exc}", flush=True)
                outcomes.append(result)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(outcomes, indent=2) + '\n', encoding='utf-8')
    return int(any(item['result'] != 'success' for item in outcomes))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('directory', type=pathlib.Path)
    proof = sub.add_parser('verify')
    proof.add_argument('--artifact-id', required=True)
    proof.add_argument('--digest', required=True)
    proof.add_argument('--platform', required=True)
    proof.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args.directory)
    else:
        sys.exit(verify(args.artifact_id, args.digest, args.platform, args.output))
