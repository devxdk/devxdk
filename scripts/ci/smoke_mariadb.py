#!/usr/bin/env python3
"""Initialize, query and stop the actual portable server on its native runner."""
import pathlib
import subprocess
import sys
import tempfile
import time


def smoke(prefix, expected):
    prefix = pathlib.Path(prefix).resolve()
    for name in ('mariadbd', 'mariadb', 'mariadb-install-db'):
        if not (prefix / 'bin' / name).is_file():
            raise RuntimeError(f'missing bin/{name}')
    version = subprocess.check_output([str(prefix / 'bin/mariadbd'), '--no-defaults', '--version'], text=True)
    if expected not in version:
        raise RuntimeError(f'wrong MariaDB version: {version}')
    with tempfile.TemporaryDirectory(prefix='mdb-') as temporary:
        work = pathlib.Path(temporary)
        data, socket = work / 'data', work / 'mysql.sock'
        subprocess.run([str(prefix / 'bin/mariadb-install-db'), '--no-defaults', f'--basedir={prefix}',
                        f'--datadir={data}', '--auth-root-authentication-method=normal'], check=True)
        with (work / 'server.log').open('wb') as output:
            process = subprocess.Popen([str(prefix / 'bin/mariadbd'), '--no-defaults', f'--basedir={prefix}',
                f'--datadir={data}', f'--socket={socket}', f'--pid-file={work / "server.pid"}', '--skip-networking'],
                stdout=output, stderr=subprocess.STDOUT)
            try:
                for _ in range(120):
                    if process.poll() is not None:
                        raise RuntimeError((work / 'server.log').read_text(errors='replace'))
                    query = subprocess.run([str(prefix / 'bin/mariadb'), '--no-defaults', f'--socket={socket}',
                        '-uroot', '-N', '-e', "CREATE DATABASE IF NOT EXISTS devxdk_smoke; USE devxdk_smoke; CREATE TABLE IF NOT EXISTS proof (id INT PRIMARY KEY) ENGINE=InnoDB; REPLACE INTO proof VALUES (42); SELECT id FROM proof;"],
                        capture_output=True, text=True)
                    if query.returncode == 0 and query.stdout.strip() == '42':
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
