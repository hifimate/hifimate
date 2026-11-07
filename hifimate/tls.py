#!/usr/bin/env python3
"""Local TLS for the LAN pages (iOS only gives microphones to HTTPS pages).

One local CA (installed and trusted once on the phone) signs a short-lived
server certificate for this host's LAN addresses and names. The CA key never
leaves this directory; nginx sees only nginx/server.{crt,key}. The server
certificate is re-issued when the addresses change or it nears expiry; the
CA stays, so the phone keeps trusting it.

usage: tls.py ensure DIR [--name HOST]   (prints "changed" when re-issued)
"""
import argparse
import ipaddress
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

# Apple rejects TLS server certificates valid for more than 398 days.
SERVER_DAYS = 397
RENEW_SECONDS = 30 * 86400
CA_DAYS = 3650
IGNORED = ('lo', 'docker', 'br-', 'veth', 'virbr', 'tun', 'tap', 'wg')


def lan_addresses(run=subprocess.run):
    """Global IPv4 addresses of real interfaces (no container bridges)."""
    out = run(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'], check=True,
              capture_output=True, text=True, timeout=5).stdout
    found = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 4 and not fields[1].startswith(IGNORED):
            address = str(ipaddress.ip_interface(fields[3]).ip)
            if address not in found:
                found.append(address)
    return found


def names(host, addresses):
    host = host.split('.')[0]
    return [f'DNS:{host}', f'DNS:{host}.local', 'DNS:localhost', 'IP:127.0.0.1'] + [f'IP:{a}' for a in addresses]


def openssl(*args, run=subprocess.run):
    run(['openssl', *args], check=True, capture_output=True, text=True, timeout=30)


def ca_name(directory, run=subprocess.run):
    """The CA's common name as iOS shows it under certificate trust settings
    (older installs made it as "HiFi Local CA"; a CA is never renamed, since the
    phones that trust it would have to install it again)."""
    result = run(['openssl', 'x509', '-noout', '-subject', '-nameopt', 'utf8,sep_comma_plus',
                  '-in', str(Path(directory) / 'ca.crt')], capture_output=True, text=True, timeout=10)
    for part in result.stdout.removeprefix('subject=').split(','):
        key, _, value = part.strip().partition('=')
        if key == 'CN':
            return value.strip()
    return None


def private(path):
    os.chmod(path, 0o600)


def ensure(directory, host=None, addresses=None, run=subprocess.run):
    directory = Path(directory)
    host = host or socket.gethostname()
    addresses = lan_addresses(run) if addresses is None else addresses
    nginx = directory / 'nginx'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    nginx.mkdir(mode=0o700, exist_ok=True)
    ca_key, ca_crt = directory / 'ca.key', directory / 'ca.crt'
    changed = False
    if not (ca_key.is_file() and ca_crt.is_file()):
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-keyout', str(ca_key), '-out', str(ca_crt), '-days', str(CA_DAYS),
                '-subj', f'/CN=HiFiMate Local CA ({host.split(".")[0]})/O=HiFiMate',
                '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign', run=run)
        private(ca_key)
        changed = True
    crt, key, san_file = nginx / 'server.crt', nginx / 'server.key', nginx / 'san.txt'
    san = ','.join(names(host, addresses))
    current = (crt.is_file() and key.is_file() and san_file.is_file() and san_file.read_text().strip() == san
               and not changed)
    if current:
        try:
            openssl('x509', '-in', str(crt), '-noout', '-checkend', str(RENEW_SECONDS), run=run)
            openssl('verify', '-CAfile', str(ca_crt), str(crt), run=run)
        except subprocess.CalledProcessError:
            current = False
    if current:
        return False
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        scratch = Path(scratch)
        extensions = scratch / 'ext.cnf'
        extensions.write_text(f'subjectAltName={san}\nbasicConstraints=critical,CA:FALSE\n'
                              'keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n')
        new_key, csr, new_crt = scratch / 'server.key', scratch / 'server.csr', scratch / 'server.crt'
        openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-keyout', str(new_key), '-out', str(csr), '-subj', f'/CN={host.split(".")[0]}', run=run)
        openssl('x509', '-req', '-in', str(csr), '-CA', str(ca_crt), '-CAkey', str(ca_key),
                '-set_serial', '0x' + secrets.token_hex(16), '-days', str(SERVER_DAYS),
                '-extfile', str(extensions), '-out', str(new_crt), run=run)
        private(new_key)
        # Renames, so nginx (bind-mounted directory) never reads a half-written pair.
        os.replace(new_key, key); os.replace(new_crt, crt)
    san_file.write_text(san + '\n')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('ensure',))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--name')
    args = parser.parse_args()
    try:
        changed = ensure(args.directory, args.name)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise SystemExit(f'TLS 证书生成失败：{error}')
    if changed:
        print('changed')


if __name__ == '__main__':
    sys.exit(main())
