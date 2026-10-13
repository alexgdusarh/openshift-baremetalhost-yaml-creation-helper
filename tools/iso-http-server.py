#!/usr/bin/env python3
"""iso-http-server.py - static file server with HTTP Range support, Python 3 stdlib only.

BMC virtual media (iDRAC, iLO, XCC, ...) streams the ISO with Range
requests; python3 -m http.server ignores Range and always sends the whole
file, which BMCs time out on or reject. This serves one directory read-only
with single-range support, as a regular user (no root, no packages).

    python3 iso-http-server.py --dir served-isos --port 8080

boot-agent-iso.yaml starts this automatically when serve_iso=true.
"""
import argparse
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)$')


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            self.send_error(403, 'Directory listing disabled')
            return None
        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, 'File not found')
            return None
        size = os.fstat(f.fileno()).st_size
        start, end = 0, size - 1
        m = RANGE_RE.match(self.headers.get('Range', '').strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(size - int(m.group(2)), 0)
            if start > end or start >= size:
                f.close()
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.end_headers()
                return None
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        else:
            self.send_response(200)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Last-Modified', self.date_time_string(os.fstat(f.fileno()).st_mtime))
        self.end_headers()
        f.seek(start)
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = self._remaining
        while remaining > 0:
            chunk = source.read(min(1 << 20, remaining))
            if not chunk:
                break
            try:
                outputfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            remaining -= len(chunk)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--dir', default='.', help='directory to serve')
    p.add_argument('--bind', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8080)
    a = p.parse_args()
    handler = partial(RangeHandler, directory=os.path.abspath(a.dir))
    srv = ThreadingHTTPServer((a.bind, a.port), handler)
    print('Serving %s on %s:%d' % (os.path.abspath(a.dir), a.bind, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
