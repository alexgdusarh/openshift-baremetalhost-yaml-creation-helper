#!/usr/bin/env python3
"""
verify_webhook.py

Receives the fact payload POSTed by verify-boot/collect-facts.sh (the
generic, host-agnostic script embedded in verify.iso - see
manage-verify-iso.yaml), identifies which host it came from by matching
the payload's observed MAC addresses against every cluster's
output/hw_inventory.yaml, cross-checks predicted_linux_ifname/wwn/
serial_number/predicted_by_path against the live-boot ground truth, and:

  - writes a full report to <cluster_dir>/output/verified/<hostname>.yaml
  - patches CONFIRMED interface-name mismatches directly into
    hw_inventory.yaml (backing up the original first, timestamped)
  - does NOT auto-patch disk-identifier mismatches (wwn/serial/by-path) -
    those are surfaced in the report only. A disk field is either right
    or it materially changes which physical disk gets selected as the
    boot device, so a human should look at MISMATCH entries there before
    anything gets overwritten - unlike an interface name, correcting it
    silently carries real risk if the MAC/host match was somehow wrong.

Deliberately stdlib-only except PyYAML (already a hard dependency for
this whole toolkit via Ansible itself) - no Flask/etc. to install here,
matches this being a small always-on service on the same box as Apache.

Usage:
  PROJECT_ROOT=/path/to/redfish-hw-inventory python3 verify_webhook.py [port]
  (defaults: PROJECT_ROOT=cwd, port=8090 / $WEBHOOK_PORT)

Or as a systemd service - see verify-webhook.service. Run standalone,
exposed directly on WEBHOOK_PORT - this is a low-traffic internal tool
that only your own BMCs ever talk to, on a private management network,
so http.server's usual "not hardened for hostile traffic" caveat doesn't
really apply, and a reverse proxy in front of it (see
apache-verify-webhook.conf) buys you TLS/access-control if you want them
but isn't needed for this to work correctly.
"""
import base64
import glob
import http.server
import json
import os
import re
import shutil
import socketserver
import sys
from datetime import datetime, timezone

import yaml

PROJECT_ROOT = os.environ.get('PROJECT_ROOT', os.getcwd())
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('WEBHOOK_PORT', 8090))


# --------------------------------------------------------------------------
# hw_inventory.yaml lookup / MAC matching
# --------------------------------------------------------------------------

def find_hw_inventory_files():
    return glob.glob(os.path.join(PROJECT_ROOT, 'clusters', '*', 'output', 'hw_inventory.yaml'))


def normalize_mac(m):
    return (m or '').strip().lower()


def extract_observed_macs(ip_addr_json):
    macs = set()
    for iface in (ip_addr_json or []):
        if iface.get('ifname') == 'lo':
            continue
        addr = normalize_mac(iface.get('address'))
        if addr:
            macs.add(addr)
    return macs


def find_host(observed_macs):
    """Search every cluster's hw_inventory.yaml for the host whose
    discovered MACs overlap the payload's observed MACs the most.
    Returns (hw_inventory_path, hostname, full_yaml_dict, overlap_count)
    or (None, None, None, 0) if nothing overlaps at all."""
    best = (None, None, None, 0)
    for path in find_hw_inventory_files():
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            print('[verify] WARNING: could not read %s: %s' % (path, e), file=sys.stderr)
            continue
        for hostname, hostdata in (data or {}).items():
            discovered = set()
            for adapter in (hostdata.get('network_adapters') or []):
                for port in (adapter.get('ports') or []):
                    mac = normalize_mac(port.get('mac_address'))
                    if mac:
                        discovered.add(mac)
            overlap = len(discovered & observed_macs)
            if overlap > best[3]:
                best = (path, hostname, data, overlap)
    return best


# --------------------------------------------------------------------------
# Payload decoding
# --------------------------------------------------------------------------

def b64_text(s):
    if not s:
        return ''
    try:
        return base64.b64decode(s).decode('utf-8', errors='replace')
    except Exception:
        return '(failed to decode)'


def parse_ethtool(raw):
    """collect-facts.sh's '== <ifname> ==' blocks -> {ifname: {driver,
    phys_port_name, pci_address}}. driver is the ground truth for
    settling a devlink_port_naming guess - see README's interface-naming
    section."""
    out = {}
    current = None
    for line in raw.splitlines():
        m = re.match(r'^== (\S+) ==$', line)
        if m:
            current = m.group(1)
            out[current] = {'driver': None, 'phys_port_name': None, 'pci_address': None}
            continue
        if current is None:
            continue
        m = re.match(r'^driver:\s*(\S+)', line)
        if m:
            out[current]['driver'] = m.group(1)
            continue
        m = re.match(r'^phys_port_name:\s*(.*)$', line)
        if m:
            v = m.group(1).strip()
            out[current]['phys_port_name'] = None if v in ('(absent)', '') else v
            continue
        m = re.match(r'^pci_address:\s*(\S+)', line)
        if m:
            out[current]['pci_address'] = m.group(1)

    return out


def build_observed_ifaces(ip_addr_json, ethtool_info):
    """{mac: {names: [ifname, *altnames], driver, phys_port_name}}"""
    out = {}
    for iface in (ip_addr_json or []):
        if iface.get('ifname') == 'lo':
            continue
        mac = normalize_mac(iface.get('address'))
        if not mac:
            continue
        names = [iface.get('ifname')] + list(iface.get('altnames') or [])
        info = ethtool_info.get(iface.get('ifname'), {})
        out[mac] = {
            'names': [n for n in names if n],
            'driver': info.get('driver'),
            'phys_port_name': info.get('phys_port_name'),
        }
    return out


# --------------------------------------------------------------------------
# Cross-checks
# --------------------------------------------------------------------------

def check_interfaces(hostdata, observed):
    results = []
    for adapter in (hostdata.get('network_adapters') or []):
        devlink_guessed = adapter.get('ports', [{}])[0].get('devlink_port_name_guessed') if adapter.get('ports') else None
        for port in (adapter.get('ports') or []):
            mac = normalize_mac(port.get('mac_address'))
            predicted = port.get('predicted_linux_ifname')
            obs = observed.get(mac)
            entry = {
                'adapter_id': adapter.get('adapter_id'),
                'port_number': port.get('port_number'),
                'mac_address': port.get('mac_address'),
                'predicted_linux_ifname': predicted,
                'devlink_port_name_guessed': port.get('devlink_port_name_guessed'),
            }
            if obs is None:
                entry['verdict'] = 'NOT_OBSERVED'
                entry['note'] = ('This MAC was not seen in the verification boot - cable '
                                  'unplugged, port disabled, or check the host match below.')
            elif predicted and predicted in obs['names']:
                entry['verdict'] = 'MATCH'
                entry['observed_names'] = obs['names']
            else:
                entry['verdict'] = 'MISMATCH'
                entry['observed_names'] = obs['names']
                entry['note'] = 'Predicted name is not among the observed name/altnames for this MAC.'
            if obs:
                entry['observed_driver'] = obs['driver']
                entry['observed_phys_port_name'] = obs['phys_port_name']
                # Ground truth for the devlink_port_naming heuristic, independent
                # of whatever the name prediction says.
                should_have_suffix = bool(obs['phys_port_name'])
                entry['devlink_port_naming_should_be'] = should_have_suffix
                if devlink_guessed is not None and devlink_guessed != should_have_suffix:
                    entry['devlink_port_naming_note'] = (
                        "devlink_port_naming is %s for this host's inventory entry, but "
                        "phys_port_name is %s on the real interface - %s inventory/hosts.yaml."
                        % ('enabled' if devlink_guessed else 'disabled',
                           'present' if should_have_suffix else 'absent',
                           'enable' if should_have_suffix else 'disable')
                    )
            results.append(entry)
    return results


def walk_lsblk(lsblk_json):
    out = []

    def walk(devs):
        for d in (devs or []):
            out.append(d)
            walk(d.get('children'))
    walk((lsblk_json or {}).get('blockdevices'))
    return out


def check_disks(hostdata, lsblk_json):
    observed_disks = walk_lsblk(lsblk_json)

    def find_observed(wwn, serial):
        for d in observed_disks:
            ow = (d.get('wwn') or '').lower()
            if wwn and ow and ow == wwn.lower():
                return d
        for d in observed_disks:
            if serial and d.get('serial') and d.get('serial') == serial:
                return d
        return None

    results = []
    storage = hostdata.get('storage', {}) or {}
    for pool in ('physical_drives', 'virtual_disks'):
        for disk in (storage.get(pool) or []):
            wwn = disk.get('wwn')
            serial = disk.get('serial_number')
            obs = find_observed(wwn, serial)
            entry = {
                'pool': pool,
                'id': disk.get('drive_id') or disk.get('volume_id'),
                'predicted_wwn': wwn,
                'predicted_serial': serial,
                'predicted_by_path': disk.get('predicted_by_path'),
            }
            if obs is None:
                entry['verdict'] = 'NOT_OBSERVED'
                entry['note'] = 'No observed disk matched this predicted wwn/serial_number.'
            else:
                ow = (obs.get('wwn') or '')
                entry['observed_name'] = '/dev/%s' % obs.get('name', '?')
                entry['observed_wwn'] = ow
                entry['observed_serial'] = obs.get('serial')
                entry['observed_size'] = obs.get('size')
                mismatches = []
                if wwn and ow and wwn.lower() != ow.lower():
                    mismatches.append('wwn')
                if serial and obs.get('serial') and serial != obs.get('serial'):
                    mismatches.append('serial_number')
                entry['verdict'] = 'MISMATCH' if mismatches else 'MATCH'
                if mismatches:
                    entry['note'] = 'Field(s) differ from observed: %s. NOT auto-corrected - review manually.' % ', '.join(mismatches)
            results.append(entry)
    return results


# --------------------------------------------------------------------------
# hw_inventory.yaml update (interfaces only - see module docstring)
# --------------------------------------------------------------------------

def apply_interface_corrections(hw_path, data, hostname, iface_results):
    changed = False
    hostdata = data[hostname]
    by_mac = {normalize_mac(r['mac_address']): r for r in iface_results}
    for adapter in (hostdata.get('network_adapters') or []):
        for port in (adapter.get('ports') or []):
            mac = normalize_mac(port.get('mac_address'))
            r = by_mac.get(mac)
            if r and r['verdict'] == 'MISMATCH' and r.get('observed_names'):
                port['predicted_linux_ifname'] = r['observed_names'][0]
                port['verified'] = True
                port['verified_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                changed = True
            elif r and r['verdict'] == 'MATCH':
                port['verified'] = True
                port['verified_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                changed = True  # timestamp/flag alone still counts as a write

    if not changed:
        return False

    backup_path = hw_path + '.bak.' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    shutil.copy2(hw_path, backup_path)
    with open(hw_path, 'w') as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    print('[verify] Updated %s (backup: %s)' % (hw_path, backup_path))
    return True


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'verify-webhook/1.0'

    def _send(self, code, body):
        data = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.rstrip('/') != '/verify':
            self._send(404, {'error': 'not found - POST to /verify'})
            return

        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except Exception as e:
            self._send(400, {'error': 'invalid JSON: %s' % e})
            return

        observed_macs = extract_observed_macs(payload.get('ip_addr'))
        hw_path, hostname, data, overlap = find_host(observed_macs)

        if hostname is None:
            print('[verify] NO MATCH for MACs: %s' % sorted(observed_macs), file=sys.stderr)
            self._send(422, {
                'error': 'Could not match this boot to any known host by MAC address',
                'observed_macs': sorted(observed_macs),
            })
            return

        ethtool_info = parse_ethtool(b64_text(payload.get('ethtool_b64')))
        observed_ifaces = build_observed_ifaces(payload.get('ip_addr'), ethtool_info)
        iface_results = check_interfaces(data[hostname], observed_ifaces)
        disk_results = check_disks(data[hostname], payload.get('lsblk'))

        report = {
            'hostname': hostname,
            'matched_by_mac_overlap': overlap,
            'verified_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'hostname_hint_from_boot': payload.get('hostname_hint'),
            'observed_macs': sorted(observed_macs),
            'interfaces': iface_results,
            'disks': disk_results,
            'multipath_raw': b64_text(payload.get('multipath_b64')),
            'fc_hosts_raw': b64_text(payload.get('fc_hosts_b64')),
            'nvme_list': payload.get('nvme_list'),
        }

        report_dir = os.path.join(os.path.dirname(os.path.dirname(hw_path)), 'output', 'verified')
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, '%s.yaml' % hostname)
        with open(report_path, 'w') as f:
            yaml.safe_dump(report, f, sort_keys=False, default_flow_style=False)

        updated = apply_interface_corrections(hw_path, data, hostname, iface_results)

        iface_mismatches = sum(1 for r in iface_results if r['verdict'] == 'MISMATCH')
        disk_mismatches = sum(1 for r in disk_results if r['verdict'] == 'MISMATCH')
        print('[verify] %s matched (%d MACs overlap). iface mismatches=%d (auto-corrected), '
              'disk mismatches=%d (report only, NOT auto-corrected). Report: %s'
              % (hostname, overlap, iface_mismatches, disk_mismatches, report_path))

        self._send(200, {
            'status': 'ok',
            'hostname': hostname,
            'report': report_path,
            'hw_inventory_updated': updated,
            'interface_mismatches': iface_mismatches,
            'disk_mismatches_needing_review': disk_mismatches,
        })

    def do_GET(self):
        if self.path.rstrip('/') == '/healthz':
            self._send(200, {'status': 'ok'})
            return
        self._send(404, {'error': 'not found'})

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))


def main():
    with socketserver.ThreadingTCPServer(('0.0.0.0', PORT), Handler) as httpd:
        print('verify_webhook listening on :%d (PROJECT_ROOT=%s)' % (PORT, PROJECT_ROOT))
        httpd.serve_forever()


if __name__ == '__main__':
    main()
