#!/usr/bin/env python3
"""Execute a PHP file through the bundled FPM, using the application's ini."""
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time


def record(kind, body=b''):
    return struct.pack('!BBHHBB', 1, kind, 1, len(body), 0, 0) + body


def length(value):
    return bytes([value]) if value < 128 else struct.pack('!I', value | 0x80000000)


def request(port, script):
    params = {'SCRIPT_FILENAME': str(script), 'SCRIPT_NAME': '/proof.php',
              'REQUEST_METHOD': 'GET', 'SERVER_PROTOCOL': 'HTTP/1.1', 'CONTENT_LENGTH': '0'}
    body = b''
    for key, value in params.items():
        key, value = key.encode(), value.encode()
        body += length(len(key)) + length(len(value)) + key + value
    with socket.create_connection(('127.0.0.1', port), timeout=2) as stream:
        stream.sendall(record(1, struct.pack('!HB5x', 1, 0)) + record(4, body) + record(4) + record(5))
        def read(size):
            value = b''
            while len(value) < size:
                chunk = stream.recv(size - len(value))
                if not chunk:
                    raise RuntimeError('FPM closed an incomplete response')
                value += chunk
            return value
        output = b''
        for _ in range(80):
            version, kind, identity, size, padding, _reserved = struct.unpack('!BBHHBB', read(8))
            if version != 1 or identity != 1:
                raise RuntimeError('FPM returned an invalid record identity')
            data = read(size)
            read(padding)
            if kind == 6:
                output += data
                if len(output) > 131072:
                    raise RuntimeError('FPM response exceeds the smoke limit')
            elif kind == 7 and data:
                raise RuntimeError('FPM stderr: ' + data.decode(errors='replace'))
            elif kind == 3:
                if len(data) != 8 or struct.unpack('!IB3x', data) != (0, 0):
                    raise RuntimeError('FPM request did not complete successfully')
                return output.partition(b'\r\n\r\n')[2].decode()
    raise RuntimeError('FPM response did not terminate')


def smoke(prefix, expected):
    prefix = pathlib.Path(prefix).resolve()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='php-fpm-proof-') as temporary:
        work = pathlib.Path(temporary)
        script, conf = work / 'proof.php', work / 'fpm.conf'
        script.write_text('<?php echo PHP_VERSION;', encoding='utf-8')
        conf.write_text(f'[global]\ndaemonize = no\nerror_log = /dev/stderr\n[www]\nlisten = 127.0.0.1:{port}\npm = static\npm.max_children = 1\n', encoding='utf-8')
        with (work / 'server.log').open('wb') as log:
            process = subprocess.Popen([str(prefix / 'sbin/php-fpm'), '-F', '-c', str(prefix / 'php.ini'), '-y', str(conf)],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                for _ in range(40):
                    if process.poll() is not None:
                        raise RuntimeError((work / 'server.log').read_text(errors='replace'))
                    try:
                        actual = request(port, script)
                    except ConnectionRefusedError:
                        time.sleep(0.25)
                        continue
                    if actual != expected:
                        raise RuntimeError(f'FPM executed PHP {actual!r}, expected {expected}')
                    print(f'smoke: PHP {expected} executed a real FastCGI request')
                    return
                raise RuntimeError('FPM did not become ready')
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=10)


if __name__ == '__main__':
    smoke(sys.argv[1], sys.argv[2])
