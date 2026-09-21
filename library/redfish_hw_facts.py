#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026
# GNU General Public License v3.0+

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: redfish_hw_facts
short_description: Discover NIC and storage hardware from a BMC via the standard Redfish API
description:
  - Vendor-neutral. Talks straight to C(/redfish/v1) using basic auth over HTTPS.
  - Works against iDRAC (Dell), iLO (HPE), XCC (Lenovo), Supermicro, or any
    DMTF Redfish 1.x compliant BMC. No vendor Python SDK or collection required.
  - Runs on the Ansible control node (use C(delegate_to=localhost)) because it
    talks HTTP(S) directly to the BMC's out-of-band management IP, not SSH to the host.
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
    no_log: true
  validate_certs:
    description: Verify the BMC's TLS certificate. Most BMCs use a self-signed cert.
    type: bool
    default: false
  use_ssl:
    description: >-
      Use HTTPS (true) or plain HTTP (false). Real BMCs (iDRAC/iLO/XCC) are
      always HTTPS. sushy-tools' Redfish emulator defaults to plain HTTP
      unless you've configured it with a cert, so lab users typically want
      this set to false.
    type: bool
    default: true
  port:
    description: >-
      TCP port to connect to. Leave unset for the protocol default
      (443/80). sushy-tools' emulator commonly listens on 8000.
    type: int
    default: null
  virtualbmc:
    description: >-
      Set true when targeting a sushy-tools libvirt/KVM virtual BMC lab
      instead of a real BMC. sushy-tools' libvirt driver keys the System
      resource by libvirt domain name (C(/redfish/v1/Systems/<domain>))
      rather than by walking the Systems collection to find a single
      generic entry. When true, the module fetches that System resource
      directly, and - because sushy-tools does not implement the
      NetworkAdapters/PCI-slot resource tree that real BMCs expose -
      falls back to the flatter C(EthernetInterfaces) resource for MAC
      addresses (no PCI slot, port count, or predicted interface name is
      available from that resource; see README).
    type: bool
    default: false
  virtual_domain:
    description: >-
      The libvirt/KVM domain name (as shown by C(virsh list --all)) for
      this VM. Required when I(virtualbmc=true); ignored otherwise.
    type: str
    default: null
  devlink_port_naming:
    description: >-
      Whether to guess a devlink-style C(npN) suffix (e.g. C(eno1np0),
      C(ens7f0np0)) on top of the ordinary predicted interface name, for
      adapters whose Redfish C(Manufacturer) string looks like Broadcom,
      Mellanox, or NVIDIA. Defaults to false (no guess) because this is
      purely a kernel driver fact (C(phys_port_name)) invisible to any
      BMC/Redfish schema - manufacturer alone cannot distinguish an older
      NIC (e.g. Broadcom's C(tg3)-driven 1GbE LOMs, which have no devlink
      support) from a newer one that does (Broadcom's C(bnxt_en)-driven
      NetXtreme-E/SmartNICs, or Mellanox/NVIDIA C(mlx5_core)) - confirmed
      by testing against real hardware from both families. Enable this
      per host, after confirming via a live boot (C(ip addr), altnames
      specifically - they show systemd's own name even if something else
      later renamed the interface) that this host's NIC driver actually
      exposes C(phys_port_name). A wrong guess here breaks agent-config
      interface matching outright, so this defaults to the safer, more
      conservative choice.
    type: bool
    default: false
author:
  - Claude
'''

EXAMPLES = r'''
- name: Discover hardware from a real iDRAC
  redfish_hw_facts:
    baseuri: 192.168.1.10
    username: root
    password: "{{ vault_bmc_password }}"
    validate_certs: false
  register: hw

- name: Discover hardware from a sushy-tools KVM lab VM
  redfish_hw_facts:
    baseuri: 192.168.122.1
    port: 8000
    use_ssl: false
    username: admin
    password: password
    virtualbmc: true
    virtual_domain: kvm-node01
  register: hw
'''

RETURN = r'''
vendor:
  description: Manufacturer string reported by the BMC/System (e.g. "Dell Inc.", "HPE", "Lenovo").
  type: str
  returned: always
network_adapters:
  description: List of NIC dictionaries. See README for full schema.
  type: list
  returned: always
storage:
  description: Dictionary with controllers, physical_drives and virtual_disks lists.
  type: dict
  returned: always
'''

import json
import re
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url


# --------------------------------------------------------------------------
# Low level Redfish GET helper
# --------------------------------------------------------------------------

def build_base_url(module):
    """Build scheme://host[:port] from baseuri/use_ssl/port. Real BMCs are
    https with no explicit port; sushy-tools labs are commonly plain http
    on a non-standard port (8000 by default)."""
    scheme = 'https' if module.params['use_ssl'] else 'http'
    host = module.params['baseuri']
    port = module.params['port']
    if port:
        return '%s://%s:%s' % (scheme, host, port)
    return '%s://%s' % (scheme, host)


def rf_get(module, base, path):
    """GET a Redfish resource. `base` is a full scheme://host[:port] string
    (see build_base_url()). `path` may be a full '@odata.id' (starts with /)
    or an absolute URL. Returns parsed JSON dict, or None on failure.

    Auth is NOT passed as call kwargs here - fetch_url() doesn't accept
    url_username/url_password/force_basic_auth/validate_certs as function
    arguments at all. It reads them internally from module.params using
    those exact key names, which main() populates before any rf_get() call.
    """
    if not path:
        return None
    if path.startswith('http'):
        url = path
    else:
        url = base.rstrip('/') + path

    headers = {'Accept': 'application/json'}
    resp, info = fetch_url(
        module, url, method='GET', headers=headers,
        timeout=module.params['timeout'],
    )
    if info.get('status') != 200 or resp is None:
        return None
    try:
        return json.loads(resp.read())
    except Exception:
        return None


def collection_members(module, base, collection):
    """Given a parsed Redfish collection object, GET and return every member."""
    if not collection:
        return []
    out = []
    for m in collection.get('Members', []):
        item = rf_get(module, base, m.get('@odata.id'))
        if item:
            out.append(item)
    return out


# --------------------------------------------------------------------------
# Linux predictable-network-interface-name prediction
# --------------------------------------------------------------------------
# Reproduces (best effort) the systemd net.ifnames scheme used by
# RHEL/CentOS/Rocky/SLES/Ubuntu-server since ~2015:
#   onboard / LOM device      -> eno<N>              (scheme 2 - firmware/BIOS index)
#   PCI hotplug slot, func 0  -> ens<slot>            (scheme 3 - ACPI/SMBIOS slot number)
#   PCI hotplug slot, func N  -> ens<slot>f<N>
#
# This is only as accurate as the slot/index data the firmware exposes to
# the BMC (Location.PartLocation.ServiceLabel / LocationOrdinalValue in the
# standard Redfish NetworkAdapter schema). Dell and HPE populate this
# reliably; some whitebox/Supermicro boards do not, in which case the
# adapter is returned with predicted_linux_ifname = null and a note.
#
# On top of that base name, systemd (>= 243, RHEL 8.3+) appends an extra
# 'np<port>' suffix - e.g. 'eno1np0', 'ens7f0np0' - whenever the NIC
# driver exposes a devlink physical port name
# (/sys/class/net/<if>/phys_port_name), which happens on SmartNIC/
# multi-host-capable silicon: confirmed on Broadcom's bnxt_en driver
# (observed directly - see the two-card, four-port dual-Broadcom example
# in README's interface-naming section) and known to also apply to
# Mellanox/NVIDIA mlx5_core. This is a KERNEL DRIVER fact
# (phys_port_name), not anything in the DMTF Redfish schema - no BMC can
# report it, so this is a vendor-string heuristic, not a certainty. Verify
# against a live boot (`ip addr`) if it matters and the vendor isn't
# Broadcom/Mellanox/NVIDIA, or if it's one of those but the suffix turns
# out to be absent anyway (older kernel, different driver/firmware combo).

ONBOARD_KEYWORDS = ('integrated', 'embedded', 'lom', 'onboard', 'builtin')

# Manufacturer substrings (case-insensitive) for NIC silicon whose Linux
# driver commonly exposes devlink phys_port_name, triggering systemd's
# 'np<port>' suffix. Best-effort, not exhaustive - see note above.
DEVLINK_PORT_NAME_VENDORS = ('broadcom', 'mellanox', 'nvidia')


def is_onboard_label(label):
    if not label:
        return False
    label = label.lower()
    return any(k in label for k in ONBOARD_KEYWORDS)


def likely_has_devlink_port_name(manufacturer):
    if not manufacturer:
        return False
    manufacturer = manufacturer.lower()
    return any(v in manufacturer for v in DEVLINK_PORT_NAME_VENDORS)


def predict_iface_name(onboard, slot_number, function_index, onboard_index,
                        devlink_port_index=None):
    """devlink_port_index: pass the port's own 0-based index within its
    adapter (the same value as function_index - NOT onboard_index, which
    is a cumulative counter across every onboard adapter in the system)
    when likely_has_devlink_port_name() says this adapter's driver is
    likely to expose one. None means "don't append a suffix" - the
    ordinary base name is returned unchanged, exactly as before this was
    added."""
    if onboard:
        idx = onboard_index if onboard_index is not None else (function_index + 1)
        name = 'eno%d' % idx
    elif slot_number is not None:
        base = 'ens%s' % slot_number
        # Normally function 0 drops the 'f0' segment entirely (systemd
        # scheme 3's usual behavior), but when a devlink phys_port_name
        # suffix ('npX') applies, systemd keeps 'f<function>' explicit
        # even for function 0 - e.g. 'ens7f0np0', NOT 'ens7np0'.
        # Confirmed against a real two-port Broadcom card's `ip addr`
        # output (see README's interface-naming section) - this isn't a
        # guess about that part.
        if function_index == 0 and devlink_port_index is None:
            name = base
        else:
            name = '%sf%d' % (base, function_index)
    else:
        return None
    if devlink_port_index is not None:
        name = '%snp%d' % (name, devlink_port_index)
    return name


def port_number_from_id(port_id):
    """Best-effort fallback for BMCs that leave PhysicalPortNumber null on
    the consolidated NetworkAdapters/Ports schema, even though the
    property is standard DMTF Redfish - a known gap on Dell iDRAC9
    (observed on a PowerEdge R240's embedded Broadcom NIC, and reported
    on other iDRAC9 systems/NIC models too - not model-specific).

    Dell's own Port resource Id/FQDD still encodes the port number
    positionally even when PhysicalPortNumber is empty:
    '<adapter-fqdd>-<port>-<function>', e.g. 'NIC.Embedded.1-1-1' is
    port 1, 'NIC.Embedded.1-2-1' is port 2. Extract it from there.

    Returns None (not a guess) if the Id doesn't match that shape - the
    caller falls back further, to the port's position in the Ports
    collection.
    """
    if not port_id:
        return None
    parts = port_id.split('-')
    if len(parts) >= 2 and parts[-2].isdigit():
        return int(parts[-2])
    return None


# --------------------------------------------------------------------------
# Network discovery
# --------------------------------------------------------------------------

def discover_network_ethernetinterfaces(module, base, virtual_domain):
    """sushy-tools (libvirt driver) does NOT implement the DMTF
    NetworkAdapters/NetworkDeviceFunctions/Ports resource tree that real
    BMCs (iDRAC/iLO/XCC) expose - per the sushy-tools docs, its network
    resource is the older, flatter EthernetInterfaces collection under the
    System (available when the emulator's feature set is 'vmedia' or
    'full'; the 'minimum' feature set has no network resource at all).
    EthernetInterfaces gives one MAC per virtual NIC with no PCI slot,
    no port grouping, and no adapter/card concept, so this returns a
    reduced-fidelity result clearly flagged as such - there is no PCI
    slot data to predict a systemd interface name from."""
    adapters_out = []
    eth_col = rf_get(module, base, '/redfish/v1/Systems/%s/EthernetInterfaces' % virtual_domain)
    for iface in collection_members(module, base, eth_col):
        mac = iface.get('MACAddress') or iface.get('PermanentMACAddress')
        adapters_out.append({
            'adapter_id': iface.get('Id'),
            'model': None,
            'manufacturer': None,
            'part_number': None,
            'serial_number': None,
            'onboard': None,
            'pci_slot': None,
            'pci_slot_service_label': None,
            'num_ports': 1,
            'ports': [{
                'port_number': 1,
                'mac_address': mac,
                'link_status': 'LinkUp' if iface.get('LinkStatus') == 'LinkUp' else iface.get('LinkStatus'),
                'predicted_linux_ifname': None,
            }],
            'source': 'EthernetInterfaces',
            'note': ("sushy-tools/KVM lab: this VM's virtual NIC has no PCI slot or "
                     "adapter data in Redfish, so a systemd interface name cannot be "
                     "predicted the way it can for real hardware. Cross-check the "
                     "guest's actual NIC naming with 'virsh domiflist %s' or inside "
                     "the guest OS." % virtual_domain),
        })
    return adapters_out


def discover_network(module, base, virtual_domain=None, devlink_port_naming=False):
    """virtual_domain: when set (sushy-tools/KVM lab mode), skip the
    Chassis/NetworkAdapters path entirely and query EthernetInterfaces
    instead - see discover_network_ethernetinterfaces() docstring for why.
    sushy-tools' Chassis resource is a shared/static, imaginary resource
    unrelated to any individual VM (not keyed by domain name at all), and
    it doesn't expose NetworkAdapters regardless.

    devlink_port_naming: opt-in (see the module's DOCUMENTATION for
    devlink_port_naming) - only applied on top of the manufacturer check
    (likely_has_devlink_port_name), never on its own."""
    if virtual_domain:
        return discover_network_ethernetinterfaces(module, base, virtual_domain)

    adapters_out = []
    onboard_counter = [0]  # mutable counter shared across adapters/ports

    chassis_col = rf_get(module, base, '/redfish/v1/Chassis')
    chassis_list = collection_members(module, base, chassis_col)

    for chassis in chassis_list:
        na_ref = chassis.get('NetworkAdapters', {}).get('@odata.id')
        if not na_ref:
            continue
        na_col = rf_get(module, base, na_ref)

        for adapter in collection_members(module, base, na_col):
            loc = adapter.get('Location', {}).get('PartLocation', {}) or {}
            service_label = loc.get('ServiceLabel')
            slot_ordinal = loc.get('LocationOrdinalValue')
            onboard = is_onboard_label(service_label) or (
                service_label is None and slot_ordinal is None
            )
            slot_number = None if onboard else (
                slot_ordinal if slot_ordinal is not None else service_label
            )

            # --- Ports / MACs -------------------------------------------------
            ports_raw = []
            if 'Ports' in adapter:  # 2021+ consolidated schema
                port_col = rf_get(module, base, adapter['Ports']['@odata.id'])
                for p in collection_members(module, base, port_col):
                    macs = p.get('Ethernet', {}).get('AssociatedMACAddresses', [])
                    # PhysicalPortNumber is standard DMTF Redfish, but some
                    # BMCs (notably Dell iDRAC9) leave it null even on this
                    # schema - fall back to parsing it from the port's own
                    # Id/FQDD (see port_number_from_id()'s docstring); the
                    # final positional fallback below covers anything that
                    # still comes back None (e.g. a non-Dell BMC with the
                    # same gap and a differently-shaped Id).
                    port_number = p.get('PhysicalPortNumber')
                    if port_number is None:
                        port_number = port_number_from_id(p.get('Id'))
                    ports_raw.append({
                        'port_number': port_number,
                        'mac_address': macs[0] if macs else None,
                        'link_status': p.get('LinkStatus'),
                    })
            elif 'NetworkDeviceFunctions' in adapter:  # older split schema
                ndf_col = rf_get(module, base, adapter['NetworkDeviceFunctions']['@odata.id'])
                for f in collection_members(module, base, ndf_col):
                    eth = f.get('Ethernet', {}) or {}
                    mac = eth.get('MACAddress') or eth.get('PermanentMACAddress')
                    phys_port = f.get('PhysicalPortAssignment')
                    ports_raw.append({
                        'port_number': phys_port if phys_port is not None else len(ports_raw) + 1,
                        'mac_address': mac,
                        'link_status': None,
                    })

            ports_out = []
            devlink_ports = devlink_port_naming and likely_has_devlink_port_name(adapter.get('Manufacturer'))
            for idx, p in enumerate(ports_raw):
                if onboard:
                    onboard_counter[0] += 1
                    onboard_idx = onboard_counter[0]
                else:
                    onboard_idx = None
                ports_out.append({
                    # Final fallback if both PhysicalPortNumber and Id
                    # parsing came back None: the port's own position in
                    # the (already-ordered) Ports/NetworkDeviceFunctions
                    # collection, 1-indexed. Not authoritative like a
                    # BMC-reported number, but better than null - Redfish
                    # collections are returned in a stable, meaningful
                    # order for these resources.
                    'port_number': p['port_number'] if p['port_number'] is not None else idx + 1,
                    'mac_address': p['mac_address'],
                    'link_status': p['link_status'],
                    'predicted_linux_ifname': predict_iface_name(
                        onboard, slot_number, idx, onboard_idx,
                        devlink_port_index=idx if devlink_ports else None,
                    ),
                    # Surfaced so a Broadcom/Mellanox/NVIDIA adapter whose
                    # driver DIDN'T expose phys_port_name (or a different
                    # vendor whose driver unexpectedly does) is easy to
                    # spot and correct with a per-host 'name:' override in
                    # inventory/hosts.yaml - see README's interface-naming
                    # section.
                    'devlink_port_name_guessed': devlink_ports,
                })

            adapters_out.append({
                'adapter_id': adapter.get('Id'),
                'model': adapter.get('Model'),
                'manufacturer': adapter.get('Manufacturer'),
                'part_number': adapter.get('PartNumber'),
                'serial_number': adapter.get('SerialNumber'),
                'onboard': onboard,
                'pci_slot': slot_number,
                'pci_slot_service_label': service_label,
                'num_ports': len(ports_out),
                'ports': ports_out,
            })

    return adapters_out


# --------------------------------------------------------------------------
# Boot-option cross reference (to flag which volume/drive is the boot device)
# --------------------------------------------------------------------------

def boot_reference_ids(module, base, system):
    """Return a set of @odata.id-ish strings referenced by the current UEFI
    boot order, so we can flag a volume/drive as bootable. Best effort -
    not every BMC populates BootOptions with a resolvable device link."""
    refs = set()
    boot = system.get('Boot', {}) or {}
    boot_order = boot.get('BootOrder', [])
    bo_ref = system.get('BootOptions', {}).get('@odata.id') if 'BootOptions' in system else None
    if not bo_ref:
        return refs
    bo_col = rf_get(module, base, bo_ref)
    for opt in collection_members(module, base, bo_col):
        if boot_order and opt.get('BootOptionReference') not in boot_order:
            continue
        # UefiDevicePath often embeds the controller/volume path or a string
        # like "RAID.Integrated.1-1" / "Disk.Virtual.0:RAID.Integrated.1-1"
        udp = opt.get('UefiDevicePath', '') or ''
        desc = opt.get('Description', '') or ''
        refs.add(udp)
        refs.add(desc)
        refs.add(opt.get('DisplayName', ''))
    return refs


def looks_bootable(identifiers, boot_refs):
    for ident in identifiers:
        if not ident:
            continue
        for ref in boot_refs:
            if ref and ident.lower() in ref.lower():
                return True
    return False


# --------------------------------------------------------------------------
# Storage discovery
# --------------------------------------------------------------------------

def oem_bus_device_function(obj):
    """Best-effort extraction of raw PCI B:D:F from vendor OEM extensions.
    Returns (bus, device, function) as ints, or (None, None, None) if the
    firmware does not expose it. Standard Redfish does NOT guarantee this -
    see README 'Known limitation' section."""
    oem = obj.get('Oem', {}) or {}
    dell = oem.get('Dell', {}) or {}
    hpe = oem.get('Hpe', {}) or {}

    for blob in (dell.get('DellController', {}), dell.get('DellPCIeFunction', {}), hpe.get('DeviceInstance', {})):
        if not blob:
            continue
        bus = blob.get('Bus') or blob.get('PciBusNumber')
        dev = blob.get('Device') or blob.get('PciDeviceNumber')
        func = blob.get('Function') or blob.get('PciFunctionNumber')
        if bus is not None and dev is not None:
            return bus, dev, func or 0
    return None, None, None


def build_by_path(bus, device, function, host_index, target, lun=0, domain=0):
    if bus is None or device is None:
        return None
    pci = '%04x:%02x:%02x.%x' % (domain, int(bus), int(device), int(function or 0))
    return '/dev/disk/by-path/pci-%s-scsi-%d:0:%s:%d' % (pci, host_index, target, lun)


def durable_identifier(obj):
    """Best-effort extraction of a hardware-intrinsic durable identifier
    (WWN/NAA/EUI/etc.) from a Redfish resource's standard C(Identifiers)
    array, when the BMC populates one. This is the SAME identifier the
    installed OS reports for that device (udevadm/lsblk ID_WWN, or
    /sys/block/*/device/wwid) - unlike predicted_by_path, it requires no
    prediction of Linux's PCI/SCSI enumeration at all, so it can't be
    wrong the way a by-path guess can.

    Returns (value, format) or (None, None) if absent. NOT guaranteed
    present - coverage varies a lot by vendor/controller, especially for
    RAID virtual Volumes (a physical Drive's SerialNumber, captured
    separately below, is far more consistently populated than a Volume's
    Identifiers). Callers that feed this into rootDeviceHints.wwn should
    run the NAA-format result through normalize_wwn() first - see its
    docstring."""
    for ident in (obj.get('Identifiers') or []):
        name = ident.get('DurableName')
        fmt = ident.get('DurableNameFormat')
        if name:
            return name, fmt
    return None, None


def normalize_wwn(value, fmt):
    """DMTF Redfish's Identifiers[].DurableName for NAA-format WWNs is
    bare hex, no prefix (e.g. '50014ee0adfe2945') - but Linux/udev
    (ID_WWN, /dev/disk/by-id/wwn-*) and Ironic/Metal3's
    rootDeviceHints.wwn BOTH expect the '0x'-prefixed form
    ('0x50014ee0adfe2945'), confirmed against real udevadm/lsblk output
    for the same physical disk, and against every official Metal3/Ironic
    rootDeviceHints example (all show wwn: "0x..."). Ironic's hint match
    is an EXACT string compare (s==), so a missing '0x' silently fails to
    match any disk at install time rather than erroring - this closes
    that gap. Only touches NAA format (the common case for SAS/SATA); EUI
    and other formats aren't known to need this and are passed through
    unchanged. Idempotent - a value that already has '0x' (any case) is
    left alone."""
    if value and (fmt or '').upper() == 'NAA' and not value.lower().startswith('0x'):
        return '0x' + value
    return value


def discover_storage(module, base, system):
    controllers_out = []
    drives_out = []
    volumes_out = []

    boot_refs = boot_reference_ids(module, base, system)

    storage_ref = system.get('Storage', {}).get('@odata.id')
    if not storage_ref:
        return {'controllers': [], 'physical_drives': [], 'virtual_disks': []}

    storage_col = rf_get(module, base, storage_ref)

    for host_index, storage in enumerate(collection_members(module, base, storage_col)):
        ctrl_list = storage.get('StorageControllers', [])
        ctrl_name = storage.get('Id')
        ctrl_model = None
        bus = device = function = None

        for ctrl in ctrl_list:
            ctrl_model = ctrl.get('Model') or ctrl_model
            b, d, f = oem_bus_device_function(ctrl)
            bus, device, function = bus or b, device or d, function or f

        pci_bdf_known = bus is not None and device is not None
        controllers_out.append({
            'controller_id': ctrl_name,
            'model': ctrl_model,
            'scsi_host_index': host_index,
            'pci_bus': bus,
            'pci_device': device,
            'pci_function': function,
            'pci_bdf_source': 'oem' if pci_bdf_known else 'unknown',
        })

        # ---- physical drives -------------------------------------------------
        drive_refs = storage.get('Drives', [])
        for dm in drive_refs:
            drive = rf_get(module, base, dm.get('@odata.id'))
            if not drive:
                continue
            ploc = drive.get('PhysicalLocation', {}).get('PartLocation', {}) or {}
            target = ploc.get('LocationOrdinalValue')
            if target is None:
                # fall back to trailing digits in the drive Id, e.g. "Disk.Bay.3"
                m = re.search(r'(\d+)$', drive.get('Id', '') or '')
                target = int(m.group(1)) if m else 0

            member_of_volume = bool(drive.get('Links', {}).get('Volumes'))
            by_path = build_by_path(bus, device, function, host_index, target)
            wwn, wwn_format = durable_identifier(drive)
            wwn = normalize_wwn(wwn, wwn_format)

            drives_out.append({
                'drive_id': drive.get('Id'),
                'model': drive.get('Model'),
                'serial_number': drive.get('SerialNumber'),
                'wwn': wwn,
                'wwn_format': wwn_format,
                'media_type': drive.get('MediaType'),
                'protocol': drive.get('Protocol'),
                'capacity_bytes': drive.get('CapacityBytes'),
                'controller_id': ctrl_name,
                'scsi_target': target,
                'part_of_virtual_disk': member_of_volume,
                'predicted_by_path': by_path,
                'by_path_confidence': 'high' if pci_bdf_known else 'best_effort_pci_bdf_unknown',
                'bootable': looks_bootable(
                    [drive.get('Id'), drive.get('SerialNumber')], boot_refs
                ) if not member_of_volume else False,
            })

        # ---- virtual disks / volumes ------------------------------------------
        vol_ref = storage.get('Volumes', {}).get('@odata.id')
        if vol_ref:
            # For inheriting identifiers from a single-member pass-through
            # volume below - only this controller's own drives, keyed by
            # their Redfish Id.
            drives_this_ctrl = {d['drive_id']: d for d in drives_out
                                 if d['controller_id'] == ctrl_name}
            vol_col = rf_get(module, base, vol_ref)
            for v_index, vol in enumerate(collection_members(module, base, vol_col)):
                member_drive_ids = [
                    d.get('@odata.id', '').rstrip('/').split('/')[-1]
                    for d in vol.get('Links', {}).get('Drives', [])
                ]
                by_path = build_by_path(bus, device, function, host_index, v_index)
                wwn, wwn_format = durable_identifier(vol)
                wwn = normalize_wwn(wwn, wwn_format)
                serial_number = None

                # A RAWDEVICE/pass-through volume (e.g. a plain AHCI disk
                # with no real RAID - Dell's iDRAC still wraps it in a
                # Volume) wrapping exactly one physical drive IS that
                # drive, as far as the OS is concerned - inherit its wwn/
                # serial_number when the volume's own Identifiers didn't
                # have one, rather than leaving select_boot_disk() to fall
                # back to a much weaker by-path/sda guess for a disk whose
                # real identifiers were sitting right there in the
                # physical Drive entry.
                if not wwn and len(member_drive_ids) == 1:
                    member_drive = drives_this_ctrl.get(member_drive_ids[0])
                    if member_drive:
                        wwn = member_drive.get('wwn')
                        wwn_format = member_drive.get('wwn_format')
                        serial_number = member_drive.get('serial_number')

                volumes_out.append({
                    'volume_id': vol.get('Id'),
                    'name': vol.get('Name'),
                    'raid_type': vol.get('RAIDType') or vol.get('VolumeType'),
                    'capacity_bytes': vol.get('CapacityBytes'),
                    'controller_id': ctrl_name,
                    'member_physical_drives': member_drive_ids,
                    'scsi_target': v_index,
                    'predicted_by_path': by_path,
                    'wwn': wwn,
                    'wwn_format': wwn_format,
                    'serial_number': serial_number,
                    'by_path_confidence': 'high' if pci_bdf_known else 'best_effort_pci_bdf_unknown',
                    'bootable': looks_bootable(
                        [vol.get('Id'), vol.get('Name')], boot_refs
                    ),
                })

    return {
        'controllers': controllers_out,
        'physical_drives': drives_out,
        'virtual_disks': volumes_out,
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    module = AnsibleModule(
        argument_spec=dict(
            baseuri=dict(type='str', required=True),
            username=dict(type='str', required=True),
            password=dict(type='str', required=True, no_log=True),
            validate_certs=dict(type='bool', default=False),
            use_ssl=dict(type='bool', default=True),
            port=dict(type='int', default=None),
            timeout=dict(type='int', default=30),
            virtualbmc=dict(type='bool', default=False),
            virtual_domain=dict(type='str', default=None),
            devlink_port_naming=dict(type='bool', default=False),
        ),
        required_if=[
            ('virtualbmc', True, ['virtual_domain']),
        ],
        supports_check_mode=True,
    )

    # fetch_url() takes no auth/TLS kwargs directly - it reads them from
    # module.params using these exact key names. They're not part of our
    # public argument_spec (which uses username/password for a friendlier
    # module interface), so wire them in here before any rf_get() call.
    module.params['url_username'] = module.params['username']
    module.params['url_password'] = module.params['password']
    module.params['force_basic_auth'] = True
    # validate_certs is already present under that exact name - nothing to do.

    base = build_base_url(module)
    virtualbmc = module.params['virtualbmc']
    domain = module.params['virtual_domain']

    root = rf_get(module, base, '/redfish/v1/')
    if root is None:
        module.fail_json(msg='Could not reach Redfish root at %s/redfish/v1/ '
                              '- check IP/port, credentials, use_ssl, and validate_certs.' % base)

    if virtualbmc:
        # sushy-tools libvirt driver: System resource is keyed by the
        # libvirt domain name, not discoverable via a normal collection walk.
        system = rf_get(module, base, '/redfish/v1/Systems/%s' % domain)
        if system is None:
            module.fail_json(
                msg='No System resource at /redfish/v1/Systems/%s - check that '
                    'virtual_domain exactly matches the libvirt domain name '
                    '(see: virsh list --all). Names are case-sensitive.' % domain
            )
    else:
        systems_col = rf_get(module, base, '/redfish/v1/Systems')
        systems = collection_members(module, base, systems_col)
        if not systems:
            module.fail_json(msg='No Systems resource returned by BMC at %s' % base)
        system = systems[0]  # bare-metal servers have exactly one ComputerSystem

    vendor = system.get('Manufacturer') or root.get('Vendor') or 'unknown'

    network_adapters = discover_network(module, base, virtual_domain=domain if virtualbmc else None,
                                         devlink_port_naming=module.params['devlink_port_naming'])
    storage = discover_storage(module, base, system)

    module.exit_json(
        changed=False,
        vendor=vendor,
        model=system.get('Model'),
        service_tag_or_serial=system.get('SerialNumber'),
        network_adapters=network_adapters,
        storage=storage,
    )


if __name__ == '__main__':
    main()
