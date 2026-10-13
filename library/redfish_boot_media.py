#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026
# GNU General Public License v3.0+

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: redfish_boot_media
short_description: Mount an ISO as Redfish virtual media, set a one-time CD boot, and reset the server
description:
  - Vendor-neutral. Discovers the ComputerSystem, its Manager and a CD/DVD-capable
    VirtualMedia slot by walking the Redfish tree instead of assuming vendor paths,
    so it works on iDRAC (both the older Managers/.../VirtualMedia and the newer
    Systems/.../VirtualMedia layout), iLO, XCC, Supermicro, and sushy-tools.
  - Idempotent per step - an ISO that is already mounted with the same URL is left
    alone, a different one is ejected first; the boot override is only patched when
    it is not already Once/Cd.
  - Supports check mode, reporting what it would do without changing anything.
options:
  baseuri:
    description: BMC IP address or hostname (no scheme, no trailing slash).
    required: true
    type: str
  username:
    description: BMC login username.
    required: true
    type: str
  password:
    description: BMC login password.
    required: true
    type: str
  validate_certs:
    type: bool
    default: false
  use_ssl:
    type: bool
    default: true
  port:
    type: int
    default: null
  timeout:
    description: Per-request timeout in seconds.
    type: int
    default: 60
  state:
    description:
      - C(booted) - mount I(image_url), set one-time boot to CD, then reset per I(power).
      - C(mounted) - mount I(image_url) and set the one-time boot, no reset.
      - C(ejected) - eject whatever is mounted in the selected slot.
    choices: [booted, mounted, ejected]
    default: booted
  image_url:
    description: URL the BMC itself fetches the ISO from (http/https/nfs/cifs). Required unless state=ejected.
    type: str
  power:
    description:
      - C(restart) - ForceRestart a powered-on server, power on a powered-off one.
      - C(on) - only power on servers that are off; leave running ones alone.
      - C(off) - with I(state=ejected) only, ForceOff servers that are on (abort/cleanup).
    choices: [restart, on, off]
    default: restart
  system_id:
    description: >-
      Id, Name or UUID of the System to use. Required when the BMC exposes
      more than one System (sushy-tools: the libvirt domain name). Default is
      the single System.
    type: str
  virtual_media_id:
    description: Force a VirtualMedia member id (e.g. C(CD), C(1), C(2)) instead of picking the first CD/DVD slot.
    type: str
  wait_timeout:
    description: Seconds to wait for eject/insert to show up on the VirtualMedia resource.
    type: int
    default: 120
'''

RETURN = r'''
system: {description: '@odata.id of the System used', type: str, returned: always}
virtual_media: {description: '@odata.id of the VirtualMedia slot used', type: str, returned: always}
power_state_before: {description: PowerState before any reset, type: str, returned: always}
actions: {description: Steps taken (or that would be taken in check mode), type: list, returned: always}
'''

import json
import time
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url


class Redfish(object):
    def __init__(self, module):
        self.module = module
        p = module.params
        scheme = 'https' if p['use_ssl'] else 'http'
        self.base = '%s://%s' % (scheme, p['baseuri']) + (':%s' % p['port'] if p['port'] else '')
        # fetch_url() reads auth/TLS from module.params under these names.
        p['url_username'] = p['username']
        p['url_password'] = p['password']
        p['force_basic_auth'] = True

    def _url(self, path):
        return path if path.startswith('http') else self.base + path

    def request(self, method, path, body=None, ok=(200, 201, 202, 204)):
        headers = {'Accept': 'application/json'}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body)
        resp, info = fetch_url(self.module, self._url(path), method=method, data=data,
                               headers=headers, timeout=self.module.params['timeout'])
        status = info.get('status')
        raw = resp.read() if resp is not None else info.get('body', b'')
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        if status not in ok:
            msg = parsed.get('error', {}).get('@Message.ExtendedInfo', [{}])
            msg = (msg[0].get('Message') if msg else None) or info.get('msg') or raw
            self.module.fail_json(msg='%s %s -> HTTP %s: %s' % (method, path, status, msg))
        return parsed

    def get(self, path):
        return self.request('GET', path, ok=(200,))

    def members(self, path):
        return [self.get(m['@odata.id']) for m in self.get(path).get('Members', [])]


def action_target(resource, name):
    """Actions['#VirtualMedia.InsertMedia'].target, or None."""
    return (resource.get('Actions', {}).get('#' + name) or {}).get('target')


def is_cd(vm):
    types = [t.upper() for t in vm.get('MediaTypes', [])]
    return any(t in ('CD', 'DVD') for t in types) or vm.get('Id', '').upper() in ('CD', 'DVD')


def find_system(rf, system_id):
    systems = rf.get('/redfish/v1/Systems').get('Members', [])
    if system_id:
        # Match on Id, Name or UUID: sushy-tools keys Systems by libvirt UUID
        # but names them after the domain; real BMCs use Id in the URL.
        for m in systems:
            s = rf.get(m['@odata.id'])
            if system_id in (s.get('Id'), s.get('Name'), s.get('UUID')):
                return s
        rf.module.fail_json(msg='No System with Id/Name/UUID %s (have: %s)'
                                % (system_id, ', '.join(m['@odata.id'] for m in systems)))
    if len(systems) != 1:
        rf.module.fail_json(msg='BMC exposes %d Systems (%s) - set system_id to pick one.'
                                % (len(systems), ', '.join(m['@odata.id'] for m in systems)))
    return rf.get(systems[0]['@odata.id'])


def find_virtual_media(rf, system, wanted_id):
    """Newer firmware (iDRAC9 6.x+, Redfish 1.13+) puts VirtualMedia under the
    System; older firmware and most other vendors under its Manager. Look in
    both, System first."""
    sources = []
    if system.get('VirtualMedia'):
        sources.append(system['VirtualMedia']['@odata.id'])
    managers = [m['@odata.id'] for m in system.get('Links', {}).get('ManagedBy', [])]
    if not managers:
        managers = [m['@odata.id'] for m in rf.get('/redfish/v1/Managers').get('Members', [])]
    for mgr in managers:
        vm = rf.get(mgr).get('VirtualMedia')
        if vm:
            sources.append(vm['@odata.id'])

    candidates = []
    for src in sources:
        candidates.extend(rf.members(src))
    if wanted_id:
        candidates = [vm for vm in candidates if vm.get('Id') == wanted_id]
    else:
        candidates = [vm for vm in candidates if is_cd(vm)]
    if not candidates:
        rf.module.fail_json(msg='No %s VirtualMedia slot found under %s'
                                % ('Id=%s' % wanted_id if wanted_id else 'CD/DVD', ', '.join(sources) or 'System/Managers'))
    return candidates[0]


def wait_for(rf, path, predicate, what):
    deadline = time.time() + rf.module.params['wait_timeout']
    while True:
        vm = rf.get(path)
        if predicate(vm):
            return vm
        if time.time() > deadline:
            rf.module.fail_json(
                msg='Timed out waiting for %s on %s (Inserted=%s, Image=%s). If inserting, the '
                    'BMC itself must reach the image URL from its own management network.'
                    % (what, path, vm.get('Inserted'), vm.get('Image')))
        time.sleep(5)


def eject(rf, vm, actions, check):
    path = vm['@odata.id']
    actions.append('eject %s' % (vm.get('Image') or '(unknown image)'))
    if check:
        return
    target = action_target(vm, 'VirtualMedia.EjectMedia')
    if target:
        rf.request('POST', target, body={}, ok=(200, 202, 204, 400))
    else:
        rf.request('PATCH', path, body={'Image': None, 'Inserted': False})
    wait_for(rf, path, lambda v: not v.get('Inserted'), 'eject')


def insert(rf, vm, url, actions, check):
    path = vm['@odata.id']
    actions.append('insert %s' % url)
    if check:
        return
    target = action_target(vm, 'VirtualMedia.InsertMedia')
    if target:
        # Just Image: iDRAC rejects some optional fields (Inserted/
        # WriteProtected) on older firmware, and they default sensibly.
        rf.request('POST', target, body={'Image': url})
    else:
        rf.request('PATCH', path, body={'Image': url, 'Inserted': True, 'WriteProtected': True})
    wait_for(rf, path, lambda v: v.get('Inserted') and v.get('Image') == url, 'insert')


def set_boot_once_cd(rf, system, actions, check):
    boot = system.get('Boot', {})
    if boot.get('BootSourceOverrideEnabled') == 'Once' and boot.get('BootSourceOverrideTarget') == 'Cd':
        return
    body = {'BootSourceOverrideEnabled': 'Once', 'BootSourceOverrideTarget': 'Cd'}
    # Restate the current UEFI/Legacy mode: some BMCs reset it to their
    # default when a PATCH omits it.
    if boot.get('BootSourceOverrideMode'):
        body['BootSourceOverrideMode'] = boot['BootSourceOverrideMode']
    actions.append('set one-time boot to Cd')
    if not check:
        rf.request('PATCH', system['@odata.id'], body={'Boot': body})


def reset(rf, system, power, actions, check):
    state = system.get('PowerState')
    reset_action = system.get('Actions', {}).get('#ComputerSystem.Reset', {})
    allowed = reset_action.get('ResetType@Redfish.AllowableValues') or []
    if state == 'On':
        if power != 'restart':
            return
        reset_type = next((t for t in ('ForceRestart', 'PowerCycle', 'GracefulRestart') if not allowed or t in allowed), None)
    else:
        reset_type = 'On'
    if allowed and reset_type not in allowed:
        rf.module.fail_json(msg='No usable ResetType (allowed: %s)' % ', '.join(allowed))
    actions.append('reset %s (PowerState was %s)' % (reset_type, state))
    if not check:
        rf.request('POST', reset_action.get('target') or system['@odata.id'] + '/Actions/ComputerSystem.Reset',
                   body={'ResetType': reset_type})


def main():
    module = AnsibleModule(
        argument_spec=dict(
            baseuri=dict(type='str', required=True),
            username=dict(type='str', required=True),
            password=dict(type='str', required=True, no_log=True),
            validate_certs=dict(type='bool', default=False),
            use_ssl=dict(type='bool', default=True),
            port=dict(type='int', default=None),
            timeout=dict(type='int', default=60),
            state=dict(type='str', default='booted', choices=['booted', 'mounted', 'ejected']),
            image_url=dict(type='str', default=None),
            power=dict(type='str', default='restart', choices=['restart', 'on', 'off']),
            system_id=dict(type='str', default=None),
            virtual_media_id=dict(type='str', default=None),
            wait_timeout=dict(type='int', default=120),
        ),
        required_if=[
            ('state', 'booted', ['image_url']),
            ('state', 'mounted', ['image_url']),
        ],
        supports_check_mode=True,
    )
    p = module.params
    check = module.check_mode
    if p['power'] == 'off' and p['state'] != 'ejected':
        module.fail_json(msg='power=off is only valid with state=ejected')
    rf = Redfish(module)

    if not rf.request('GET', '/redfish/v1/', ok=(200,)):
        module.fail_json(msg='Could not reach Redfish root at %s/redfish/v1/' % rf.base)

    system = find_system(rf, p['system_id'])
    vm = find_virtual_media(rf, system, p['virtual_media_id'])
    actions = []

    if p['state'] == 'ejected':
        if vm.get('Inserted'):
            eject(rf, vm, actions, check)
        if p['power'] == 'off' and system.get('PowerState') != 'Off':
            actions.append('reset ForceOff (PowerState was %s)' % system.get('PowerState'))
            if not check:
                target = (system.get('Actions', {}).get('#ComputerSystem.Reset') or {}).get('target')
                rf.request('POST', target or system['@odata.id'] + '/Actions/ComputerSystem.Reset',
                           body={'ResetType': 'ForceOff'})
    else:
        if vm.get('Inserted') and vm.get('Image') != p['image_url']:
            eject(rf, vm, actions, check)
        if not (vm.get('Inserted') and vm.get('Image') == p['image_url']):
            insert(rf, vm, p['image_url'], actions, check)
        set_boot_once_cd(rf, system, actions, check)
        if p['state'] == 'booted':
            reset(rf, system, p['power'], actions, check)

    module.exit_json(
        changed=bool(actions),
        system=system.get('@odata.id'),
        virtual_media=vm.get('@odata.id'),
        power_state_before=system.get('PowerState'),
        actions=actions,
    )


if __name__ == '__main__':
    main()
