#!/usr/bin/env python3
"""Initialize, query and stop the actual portable server on its native runner."""
import pathlib
import os
import re
import socket as network_socket
import subprocess
import sys
import tempfile
import time


def smoke(prefix, expected):
    prefix = pathlib.Path(prefix).resolve()
    windows = sys.platform == 'win32'
    suffix = '.exe' if windows else ''
    server = prefix / 'bin' / ('mariadbd' + suffix)
    client = prefix / 'bin' / ('mariadb' + suffix)
    initializer = prefix / ('scripts' if sys.platform == 'linux' else 'bin') / ('mariadb-install-db' + suffix)
    for name in ('mariadbd' + suffix, 'mariadb' + suffix):
        if not (prefix / 'bin' / name).is_file():
            raise RuntimeError(f'missing bin/{name}')
    if not initializer.is_file():
        raise RuntimeError(f'missing initializer {initializer}')
    env = dict(os.environ)
    if sys.platform == 'linux':
        prior = env.get('LD_LIBRARY_PATH', '')
        env['LD_LIBRARY_PATH'] = str(prefix / 'lib') + (':' + prior if prior else '')
    version = subprocess.check_output([str(server), '--no-defaults', '--version'], text=True, env=env)
    reported = re.search(r'\bVer\s+([0-9]+\.[0-9]+\.[0-9]+)-MariaDB\b', version)
    if reported is None or reported[1] != expected:
        raise RuntimeError(f'wrong MariaDB version: {version}')
    with tempfile.TemporaryDirectory(prefix='mdb-') as temporary:
        work = pathlib.Path(temporary)
        data, socket = work / 'data', work / 'mysql.sock'
        if windows:
            # The EXE derives basedir from its own path and bootstraps with its
            # generated my.ini. It does not accept the Unix script's options.
            initialize = [str(initializer), f'--datadir={data}']
        else:
            initialize = [str(initializer), '--no-defaults', f'--basedir={prefix}',
                          f'--datadir={data}', '--auth-root-authentication-method=normal']
        subprocess.run(initialize, check=True, env=env)
        if windows:
            with network_socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            listener = ['--bind-address=127.0.0.1', f'--port={port}']
            connection = ['--protocol=tcp', '--host=127.0.0.1', f'--port={port}']
        else:
            listener = [f'--socket={socket}', '--skip-networking']
            connection = [f'--socket={socket}']
        with (work / 'server.log').open('wb') as output:
            process = subprocess.Popen([str(server), '--no-defaults', f'--basedir={prefix}',
                f'--datadir={data}', f'--pid-file={work / "server.pid"}', *listener],
                stdout=output, stderr=subprocess.STDOUT, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if windows else 0)
            try:
                for _ in range(120):
                    if process.poll() is not None:
                        raise RuntimeError((work / 'server.log').read_text(errors='replace'))
                    query = subprocess.run([str(client), '--no-defaults', *connection,
                        '-uroot', '-N', '-e', "CREATE DATABASE IF NOT EXISTS devxdk_smoke; USE devxdk_smoke; CREATE TABLE IF NOT EXISTS proof (id INT PRIMARY KEY) ENGINE=InnoDB; REPLACE INTO proof VALUES (42); SELECT id FROM proof;"],
                        capture_output=True, text=True, env=env, timeout=10)
                    if query.returncode == 0 and query.stdout.strip() == '42':
                        print(f'smoke: MariaDB {expected} initialized and executed an InnoDB query', flush=True)
                        return
                    time.sleep(0.5)
                raise RuntimeError(f'MariaDB query failed: {query.stderr}')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == '__main__':
    smoke(sys.argv[1], sys.argv[2])
