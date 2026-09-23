#!/usr/bin/env python3
"""
verify_webhook.py

The listener for verify-boot/collect-facts.sh's reports - that's its
only job. ISO serving is a separate, pre-existing concern: verify.iso
(built by manage-verify-iso.yaml) is served from wherever your existing
Apache/web server already serves static files from - this process never
touches that. Splitting it this way matters at real fleet scale (6-100+
servers pulling a ~1.3GB ISO concurrently during an install run): a
mature web server's static-file path (worker/event MPM, native Range/206
support) handles that load in a way a single-process Python listener
isn't built for - this listener's own traffic (small JSON POSTs, one per
verification boot) is a completely different, much lighter profile.

Receives the fact payload POSTed by collect-facts.sh (each per-host ISO
has its own unique session UUID baked in - see verify-boot/verify.bu and
mount-verify-media.yaml). The playbook registers a session (uuid +
target_host + which hw_inventory.yaml to use) BEFORE booting; the boot's
POST carries that same uuid back, so matching is a direct, unambiguous
dict lookup - no searching across every cluster's hw_inventory.yaml
guessing by MAC overlap, which could (and on real hardware, did) pick
the WRONG cluster/host when the same physical MACs happened to appear
in more than one cluster's data (e.g. a stale/duplicated hw_inventory.yaml
left over from an earlier cluster rename). A payload with no session_uuid
at all (e.g. someone booting the shared, non-per-host verify.iso
directly, outside mount-verify-media.yaml's flow) falls back to the old
MAC-search behavior - but that fallback now explicitly REJECTS an
ambiguous match (the same MAC set tied across two or more different
clusters/hosts) rather than silently picking one, surfacing every tied
candidate in the error instead.

Either way, it cross-checks predicted_linux_ifname/wwn/serial_number/
predicted_by_path against the live-boot ground truth, and:
  - writes a full report to <cluster_dir>/output/verified/<hostname>.yaml
  - patches CONFIRMED interface-name mismatches directly into
    hw_inventory.yaml (backing up the original first, timestamped)
  - does NOT auto-patch disk-identifier mismatches (wwn/serial/
    by-path) - those are surfaced in the report only. A disk field is
    either right or it materially changes which physical disk gets
    selected as the boot device, so a human should look at MISMATCH
    entries there before anything gets overwritten - unlike an
    interface name, correcting it silently carries real risk if the
    MAC/host match was somehow wrong.
  - for the session-based path specifically: also flags when the
    booted host's observed MACs have ZERO overlap with what's recorded
    for the target_host the session was registered for - since the
    session already tells us exactly which host to expect, that
    combination can only mean the hardware genuinely changed since
    discovery last ran (NIC/board swap), not an identity mismatch -
    surfaced as hardware_changed_warning in the report rather than
    silently applying corrections against the wrong expectations.

Deliberately stdlib-only except PyYAML (already a hard dependency for
this whole toolkit via Ansible itself) - no Flask/etc. to install here.

Usage:
  PROJECT_ROOT=/path/to/redfish-hw-inventory python3 verify_webhook.py [port]
  (defaults: PROJECT_ROOT=cwd, port=8090 / $WEBHOOK_PORT)

Or as a systemd service - see verify-webhook.service.j2, deployed by
deploy-verify-webhook.yaml (which also opens this port in the
firewall). Exposed directly - this is a low-traffic internal tool that
only your own BMCs ever talk to, on a private management network, so
http.server's usual "not hardened for hostile traffic" caveat doesn't
really apply here.
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
import tempfile
import threading
import time
from datetime import datetime, timezone

import yaml

PROJECT_ROOT = os.environ.get('PROJECT_ROOT', os.getcwd())
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('WEBHOOK_PORT', 8090))

# --------------------------------------------------------------------------
# Session cache: register_session()/load_session()/delete_session() below.
# A tmp file per session (not just in-memory) - deliberately durable
# across a webhook restart mid-verification, since a boot can take
# several minutes and there's no reason a webhook restart in that window
# should force the whole boot-and-wait cycle to be redone.
# --------------------------------------------------------------------------

SESSION_DIR = os.path.join(tempfile.gettempdir(), 'verify-webhook-sessions')
SESSION_MAX_AGE_SECONDS = 3600  # stale-session sweep threshold - see cleanup_stale_sessions()


def _session_path(session_uuid):
    # Defensive: the uuid always comes from Ansible's own set_fact in
    # practice, but never trust a path built from network-derived input
    # without sanitizing it first.
    safe = re.sub(r'[^a-zA-Z0-9-]', '', session_uuid or '')
    return os.path.join(SESSION_DIR, '%s.json' % safe) if safe else None


def register_session(session_uuid, data):
    os.makedirs(SESSION_DIR, exist_ok=True)
    path = _session_path(session_uuid)
    with open(path, 'w') as f:
        json.dump(data, f)


def load_session(session_uuid):
    path = _session_path(session_uuid)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def delete_session(session_uuid):
    path = _session_path(session_uuid)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def cleanup_stale_sessions():
    """Sweeps session files older than SESSION_MAX_AGE_SECONDS - covers a
    run that registered a session but then never actually booted (BMC
    error, aborted playbook run, etc.), so cache files don't accumulate
    forever. Called opportunistically on every session registration
    rather than needing a separate background thread/timer."""
    try:
        if not os.path.isdir(SESSION_DIR):
            return
        cutoff = time.time() - SESSION_MAX_AGE_SECONDS
        for name in os.listdir(SESSION_DIR):
            path = os.path.join(SESSION_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except Exception:
        pass


# --------------------------------------------------------------------------
# Live per-key status, for mount-verify-media.yaml to poll over HTTP (see
# /status/<key> in do_GET below) instead of only ever watching for the
# per-host report file. That file-based approach has a real gap: it can
# only exist once a payload has been successfully parsed AND matched to a
# hostname - a malformed payload (bad JSON, wrong Content-Length, an
# unregistered session uuid, etc.) never gets that far, so no per-host
# file can ever record it, no matter how long something polls for one.
# Recorded under BOTH the TCP source IP AND the session uuid (when one is
# present) - the session uuid is what mount-verify-media.yaml actually
# polls now (guaranteed unique per run, so a stale entry from an earlier
# unrelated attempt against the same physical IP can never answer a new
# poll), source IP stays as a secondary key for the no-session fallback
# path and for manual debugging.
#
# In-memory only, most-recent-wins - not persisted to disk (a webhook
# restart loses it, which is fine: whatever's currently mid-poll would
# just go back to waiting, and nothing here is meant to be a permanent
# audit record - the per-host YAML report already is that).
host_status = {}
host_status_lock = threading.Lock()


def record_status(key, state, detail):
    if not key:
        return
    with host_status_lock:
        host_status[key] = {
            'state': state,
            'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            **detail,
        }


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
    """FALLBACK ONLY - used when a payload has no session_uuid at all
    (e.g. the shared, non-per-host verify.iso booted directly, outside
    mount-verify-media.yaml's flow). Searches every cluster's
    hw_inventory.yaml for the host whose discovered MACs overlap the
    payload's observed MACs the most.

    Returns (hw_inventory_path, hostname, full_yaml_dict, overlap_count,
    ambiguous_candidates). ambiguous_candidates is a non-empty list of
    (path, hostname, overlap) when two or more DIFFERENT (cluster path,
    hostname) pairs are tied for the best overlap - confirmed on real
    hardware that silently picking one in that situation picks the WRONG
    one at least as often as the right one (a stale/duplicated
    hw_inventory.yaml left over from an earlier cluster rename, sharing
    the same physical MACs as the real current cluster). When
    ambiguous_candidates is non-empty, the other four fields are all
    None/0/{} - the caller must treat this as "cannot safely proceed",
    not silently fall through to the top candidate.
    """
    candidates = []
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
            if overlap > 0:
                candidates.append((path, hostname, data, overlap))

    if not candidates:
        return None, None, None, 0, []

    max_overlap = max(c[3] for c in candidates)
    top = [c for c in candidates if c[3] == max_overlap]
    if len(top) > 1:
        ambiguous = [(p, h, o) for (p, h, _d, o) in top]
        return None, None, None, 0, ambiguous

    path, hostname, data, overlap = top[0]
    return path, hostname, data, overlap, []


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


def process_matched_host(hw_path, hostname, data, payload, match_info):
    """Shared by both the session-based path and the legacy MAC-search
    fallback, once each has independently figured out WHICH host this
    is (that determination is the only thing that differs between them -
    everything after it is identical). match_info carries whatever
    match-method-specific fields belong in the report (matched_by,
    session_uuid/matched_by_mac_overlap, observed_macs, and for the
    session path, expected_macs/mac_overlap/hardware_changed_warning).

    Returns (response_body_dict, status_detail_dict) - the caller
    decides how/where to send the response and which key(s) to record
    status under.
    """
    ethtool_info = parse_ethtool(b64_text(payload.get('ethtool_b64')))
    observed_ifaces = build_observed_ifaces(payload.get('ip_addr'), ethtool_info)
    iface_results = check_interfaces(data[hostname], observed_ifaces)
    disk_results = check_disks(data[hostname], payload.get('lsblk'))

    report = {
        'hostname': hostname,
        'verified_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'hostname_hint_from_boot': payload.get('hostname_hint'),
        'interfaces': iface_results,
        'disks': disk_results,
        'multipath_raw': b64_text(payload.get('multipath_b64')),
        'fc_hosts_raw': b64_text(payload.get('fc_hosts_b64')),
        'nvme_list': payload.get('nvme_list'),
    }
    report.update(match_info)

    report_dir = os.path.join(os.path.dirname(os.path.dirname(hw_path)), 'output', 'verified')
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, '%s.yaml' % hostname)
    with open(report_path, 'w') as f:
        yaml.safe_dump(report, f, sort_keys=False, default_flow_style=False)

    updated = apply_interface_corrections(hw_path, data, hostname, iface_results)

    iface_mismatches = sum(1 for r in iface_results if r['verdict'] == 'MISMATCH')
    disk_mismatches = sum(1 for r in disk_results if r['verdict'] == 'MISMATCH')
    print('[verify] %s matched (%s). iface mismatches=%d (auto-corrected), '
          'disk mismatches=%d (report only, NOT auto-corrected). Report: %s'
          % (hostname, match_info.get('matched_by', '?'), iface_mismatches, disk_mismatches, report_path))
    if match_info.get('hardware_changed_warning'):
        print('[verify] %s' % match_info['hardware_changed_warning'], file=sys.stderr)

    response = {
        'status': 'ok',
        'hostname': hostname,
        'report': report_path,
        'hw_inventory_updated': updated,
        'interface_mismatches': iface_mismatches,
        'disk_mismatches_needing_review': disk_mismatches,
    }
    if match_info.get('hardware_changed_warning'):
        response['hardware_changed_warning'] = match_info['hardware_changed_warning']

    status_detail = {
        'hostname': hostname,
        'report_path': report_path,
        'report': report,
        'hw_inventory_updated': updated,
        'interface_mismatches': iface_mismatches,
        'disk_mismatches_needing_review': disk_mismatches,
    }
    return response, status_detail


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
        if self.path.rstrip('/') == '/session/register':
            self._handle_session_register()
            return
        if self.path.rstrip('/') != '/verify':
            self._send(404, {'error': 'not found - POST to /verify, or /session/register'})
            return

        source_ip = self.client_address[0]

        length = int(self.headers.get('Content-Length', 0))
        raw_b64 = self.rfile.read(length)

        # collect-facts.sh sends the whole payload base64-wrapped (not raw
        # JSON) specifically so a parse failure can be diagnosed directly
        # instead of guessed at - decode first, log the exact bytes on
        # ANY failure below (both the base64 as received and the decoded
        # text, when decoding itself succeeded), viewable via
        # `journalctl -u verify-webhook`.
        try:
            decoded = base64.b64decode(raw_b64, validate=True)
        except Exception as e:
            print('[verify] base64 decode failed for %s: %s' % (source_ip, e), file=sys.stderr)
            print('[verify] RAW BODY AS RECEIVED (%d bytes):' % len(raw_b64), file=sys.stderr)
            print(raw_b64.decode('utf-8', errors='replace'), file=sys.stderr)
            record_status(source_ip, 'error', {'http_status': 400, 'error': 'invalid base64: %s' % e})
            self._send(400, {'error': 'invalid base64: %s' % e})
            return

        try:
            payload = json.loads(decoded)
        except Exception as e:
            print('[verify] JSON parse failed for %s: %s' % (source_ip, e), file=sys.stderr)
            print('[verify] RAW BASE64 AS RECEIVED (%d bytes):' % len(raw_b64), file=sys.stderr)
            print(raw_b64.decode('ascii', errors='replace'), file=sys.stderr)
            print('[verify] DECODED PAYLOAD (%d bytes):' % len(decoded), file=sys.stderr)
            print(decoded.decode('utf-8', errors='replace'), file=sys.stderr)
            record_status(source_ip, 'error', {'http_status': 400, 'error': 'invalid JSON: %s' % e})
            self._send(400, {'error': 'invalid JSON: %s' % e})
            return

        session_uuid = payload.get('session_uuid')
        if session_uuid:
            self._handle_verify_with_session(payload, session_uuid, source_ip)
        else:
            self._handle_verify_legacy(payload, source_ip)

    def _handle_session_register(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except Exception as e:
            self._send(400, {'error': 'invalid JSON: %s' % e})
            return

        session_uuid = payload.get('uuid')
        hw_inventory_path = payload.get('hw_inventory_path')
        target_host = payload.get('target_host')
        if not session_uuid or not hw_inventory_path or not target_host:
            self._send(400, {'error': 'uuid, hw_inventory_path, and target_host are all required'})
            return

        cleanup_stale_sessions()
        register_session(session_uuid, {
            'uuid': session_uuid,
            'hw_inventory_path': hw_inventory_path,
            'cluster_dir': payload.get('cluster_dir'),
            'target_host': target_host,
            'registered_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        })
        print('[verify] Session registered: uuid=%s target_host=%s hw_inventory_path=%s'
              % (session_uuid, target_host, hw_inventory_path), file=sys.stderr)
        self._send(200, {'status': 'registered', 'uuid': session_uuid})

    def _handle_verify_with_session(self, payload, session_uuid, source_ip):
        session = load_session(session_uuid)
        if session is None:
            msg = ('No registered session found for uuid %s - expired, already '
                   'matched by an earlier report, or the playbook never '
                   'registered it before this boot POSTed.' % session_uuid)
            print('[verify] %s' % msg, file=sys.stderr)
            for key in (source_ip, session_uuid):
                record_status(key, 'error', {'http_status': 422, 'error': msg})
            self._send(422, {'error': msg})
            return

        hw_path = session['hw_inventory_path']
        target_host = session['target_host']

        # Always read hw_inventory.yaml FRESH here, at match time - never
        # cache its content at registration time. The window between
        # "playbook registers the session" and "boot actually POSTs" can
        # be minutes; caching the parsed content would risk matching
        # against data that's since changed (e.g. discovery re-run mid-
        # boot), exactly the class of staleness bug this whole session
        # design exists to eliminate.
        try:
            with open(hw_path) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            msg = 'Could not read %s: %s' % (hw_path, e)
            print('[verify] %s' % msg, file=sys.stderr)
            for key in (source_ip, session_uuid):
                record_status(key, 'error', {'http_status': 500, 'error': msg})
            self._send(500, {'error': msg})
            return

        if target_host not in data:
            msg = "target_host '%s' (from the registered session) is not in %s" % (target_host, hw_path)
            print('[verify] %s' % msg, file=sys.stderr)
            for key in (source_ip, session_uuid):
                record_status(key, 'error', {'http_status': 422, 'error': msg})
            self._send(422, {'error': msg})
            return

        observed_macs = extract_observed_macs(payload.get('ip_addr'))
        discovered_macs = set()
        for adapter in (data[target_host].get('network_adapters') or []):
            for port in (adapter.get('ports') or []):
                mac = normalize_mac(port.get('mac_address'))
                if mac:
                    discovered_macs.add(mac)
        mac_overlap = len(discovered_macs & observed_macs)

        # The session already tells us exactly which host to expect - so
        # zero MAC overlap here can't mean "wrong host" the way it did in
        # the old cross-cluster search (that ambiguity is structurally
        # impossible now). It can only mean the hardware itself changed
        # since discovery last ran (NIC or whole board swap). Flag it
        # rather than silently applying corrections against expectations
        # that no longer describe this physical machine.
        hardware_changed_warning = None
        if discovered_macs and mac_overlap == 0:
            hardware_changed_warning = (
                "None of this boot's observed MACs (%s) match ANY MAC recorded "
                "for '%s' in hw_inventory.yaml (%s) - since this session was "
                "registered specifically for this host, this most likely means "
                "the hardware itself changed (NIC/board swap) since discovery "
                "last ran, not a mismatched identity. Re-run discovery "
                "(playbook.yaml) against this host before trusting this report."
                % (sorted(observed_macs), target_host, sorted(discovered_macs))
            )

        match_info = {
            'matched_by': 'session_uuid',
            'session_uuid': session_uuid,
            'observed_macs': sorted(observed_macs),
            'expected_macs': sorted(discovered_macs),
            'mac_overlap': mac_overlap,
            'hardware_changed_warning': hardware_changed_warning,
        }
        response, status_detail = process_matched_host(hw_path, target_host, data, payload, match_info)

        delete_session(session_uuid)

        self._send(200, response)
        for key in (source_ip, session_uuid):
            record_status(key, 'success', status_detail)

    def _handle_verify_legacy(self, payload, source_ip):
        """No session_uuid in the payload at all - the shared, non-per-host
        verify.iso booted directly, outside mount-verify-media.yaml's
        flow. Falls back to matching by MAC search across every cluster's
        hw_inventory.yaml - see find_host()'s docstring for why an
        ambiguous match is rejected outright here rather than silently
        resolved."""
        observed_macs = extract_observed_macs(payload.get('ip_addr'))
        hw_path, hostname, data, overlap, ambiguous = find_host(observed_macs)

        if ambiguous:
            candidates_desc = ['%s: %s (overlap=%d)' % (p, h, o) for (p, h, o) in ambiguous]
            msg = (
                "This boot's MACs (%s) match %d different hosts equally well, "
                "across different clusters - cannot safely pick one: %s. This "
                "usually means a stale/duplicated hw_inventory.yaml (e.g. left "
                "over from an earlier cluster rename) - clean up the duplicate "
                "before retrying." % (sorted(observed_macs), len(ambiguous), '; '.join(candidates_desc))
            )
            print('[verify] AMBIGUOUS MATCH: %s' % msg, file=sys.stderr)
            record_status(source_ip, 'error', {'http_status': 422, 'error': msg, 'observed_macs': sorted(observed_macs)})
            self._send(422, {'error': msg, 'observed_macs': sorted(observed_macs)})
            return

        if hostname is None:
            print('[verify] NO MATCH for MACs: %s' % sorted(observed_macs), file=sys.stderr)
            record_status(source_ip, 'error', {
                'http_status': 422,
                'error': 'Could not match this boot to any known host by MAC address',
                'observed_macs': sorted(observed_macs),
            })
            self._send(422, {
                'error': 'Could not match this boot to any known host by MAC address',
                'observed_macs': sorted(observed_macs),
            })
            return

        match_info = {
            'matched_by': 'mac_search',
            'matched_by_mac_overlap': overlap,
            'observed_macs': sorted(observed_macs),
        }
        response, status_detail = process_matched_host(hw_path, hostname, data, payload, match_info)
        self._send(200, response)
        record_status(source_ip, 'success', status_detail)

    def do_GET(self):
        if self.path.rstrip('/') == '/healthz':
            self._send(200, {'status': 'ok'})
            return
        if self.path.startswith('/status/'):
            key = self.path[len('/status/'):].rstrip('/')
            with host_status_lock:
                entry = host_status.get(key)
            self._send(200, entry if entry else {'state': 'pending'})
            return
        self._send(404, {'error': 'not found - POST to /verify, or /session/register'})

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))


def main():
    with socketserver.ThreadingTCPServer(('0.0.0.0', PORT), Handler) as httpd:
        print('verify_webhook listening on :%d (PROJECT_ROOT=%s)' % (PORT, PROJECT_ROOT))
        httpd.serve_forever()


if __name__ == '__main__':
    main()
