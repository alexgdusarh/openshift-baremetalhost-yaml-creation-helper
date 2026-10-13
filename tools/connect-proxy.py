#!/usr/bin/env python3
"""connect-proxy.py - minimal HTTP CONNECT proxy, Python 3 stdlib only.

For running the playbooks from a machine that cannot reach the BMC network
directly, through a jump host that can (no root, no pip installs needed on
the jump host - just python3).

On the jump host (binds to loopback only, so nothing else can use it):
    python3 connect-proxy.py --port 3128

On the machine running Ansible:
    ssh -N -L 3128:127.0.0.1:3128 user@jumphost &
    export https_proxy=http://127.0.0.1:3128 http_proxy=http://127.0.0.1:3128
    ./run-cluster.sh <cluster-name> discover

redfish_hw_facts uses Ansible's fetch_url(), which honours https_proxy /
http_proxy, so every BMC is reached through the jump host with no inventory
changes. TLS stays end to end and the Host header keeps the BMC's real
address (iDRAC's Host Header Check rejects requests via plain ssh -L port
forwards to localhost).

Both CONNECT (https BMCs) and plain absolute-URI requests (http, e.g.
sushy-tools labs) are supported.
"""
import argparse
import select
import socket
import socketserver
import sys
from urllib.parse import urlsplit


def pipe(a, b):
    socks = [a, b]
    while True:
        r, _, x = select.select(socks, [], socks, 300)
        if x or not r:
            return
        for s in r:
            try:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
            except OSError:
                return


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = client.recv(65536)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 65536:
                return
        head, _, rest = buf.partition(b'\r\n\r\n')
        lines = head.split(b'\r\n')
        try:
            method, target, version = lines[0].decode('latin-1').split(' ', 2)
        except ValueError:
            return

        if method.upper() == 'CONNECT':
            host, _, port = target.rpartition(':')
            dest = (host.strip('[]'), int(port or 443))
            first = rest
        else:
            u = urlsplit(target)
            if not u.hostname:
                client.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
                return
            dest = (u.hostname, u.port or 80)
            path = (u.path or '/') + ('?' + u.query if u.query else '')
            hdrs = [l for l in lines[1:]
                    if not l.lower().startswith((b'proxy-', b'connection:'))]
            first = (('%s %s %s\r\n' % (method, path, version)).encode('latin-1')
                     + b'\r\n'.join(hdrs) + b'\r\nConnection: close\r\n\r\n'
                     + rest)

        try:
            upstream = socket.create_connection(dest, timeout=self.server.timeout_s)
        except OSError as e:
            sys.stderr.write('%s -> %s:%s failed: %s\n' % (method, dest[0], dest[1], e))
            client.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
            return
        upstream.settimeout(None)
        sys.stderr.write('%s %s:%s\n' % (method, dest[0], dest[1]))
        with upstream:
            if method.upper() == 'CONNECT':
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            if first:
                upstream.sendall(first)
            pipe(client, upstream)


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--bind', default='127.0.0.1',
                   help='address to listen on (default: loopback only)')
    p.add_argument('--port', type=int, default=3128)
    p.add_argument('--timeout', type=int, default=15,
                   help='upstream connect timeout in seconds')
    a = p.parse_args()
    srv = Server((a.bind, a.port), Handler)
    srv.timeout_s = a.timeout
    sys.stderr.write('CONNECT proxy listening on %s:%s\n' % (a.bind, a.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
