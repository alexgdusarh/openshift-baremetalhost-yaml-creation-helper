# -*- coding: utf-8 -*-
"""
Ansible filter plugin used by generate-agent-config.yaml.

Turns the output of redfish_hw_facts (output/hw_inventory.yaml) plus
each host's own network_members/ip (inventory/hosts.yaml) and the
cluster-wide network topology (vars/cluster.yaml `network_profile:`)
into the pieces needed for an OpenShift Assisted/Agent-based Installer
agent-config.yaml:

  select_boot_disk        -> rootDeviceHints (one of wwn/serialNumber/
                               deviceName - see its docstring for the
                               preference order and why)
  flatten_ports            -> top-level host.interfaces (name/MAC map)
  build_nmstate_interfaces -> networkConfig.interfaces (nmstate), plus a
                               'primary_interface' used to build the route.
                               link_aggregation options are validated
                               (non-blocking - see validate_link_aggregation)
                               against a per-mode table before being
                               passed through to nmstate as-is.
  dns_resolver              -> networkConfig.dns-resolver (cluster-wide)
  routes_config             -> networkConfig.routes (cluster-wide gateway,
                               per-host primary_interface from above)
  ip_in_network              -> CIDR membership check used by
                               generate-agent-config.yaml to validate
                               node IPs / apiVIPs / ingressVIPs against
                               install-config.yaml's machineNetwork

This is deliberately implemented in Python rather than Jinja: matching
members to discovered ports by (adapter_id, port_number), tracking which
ports are "used" vs need to default to state:down, and building
correctly-shaped nested nmstate dicts is error-prone to do reliably in
pure Jinja templating.
"""

import ipaddress

DEFAULT_MIN_BOOT_BYTES = 120 * (10 ** 9)  # 120 GB, decimal (matches how
                                           # drive capacity / Redfish
                                           # CapacityBytes is reported)


def _assert_hashable_port_key(key, port):
    """Defense in depth: library/redfish_hw_facts.py is now careful to
    never produce a non-scalar port_number (confirmed needed on real
    Dell iDRAC8 hardware, where the BMC itself returned a raw
    @odata.id reference object instead of an integer for
    PhysicalPortNumber), but a genuinely unknown vendor/firmware quirk
    could still slip an unhashable value like a dict or list through
    some day. Fails with a clear, actionable message pointing at
    exactly which port - instead of a bare "unhashable type: 'dict'"
    with no indication of where it came from."""
    try:
        hash(key)
    except TypeError:
        raise ValueError(
            "port_number for adapter '%s' port '%s' is not a plain value "
            "(got %r) - check hw_inventory.yaml directly; this usually "
            "means the BMC returned something unexpected for this port's "
            "Redfish PhysicalPortNumber/Id and needs a targeted fix in "
            "library/redfish_hw_facts.py, not here."
            % (key[0], port.get('port_number'), port.get('port_number'))
        )


# --------------------------------------------------------------------------
# link_aggregation option validation
# --------------------------------------------------------------------------
# network_profile.link_aggregation is passed through to nmstate as-is
# (see build_nmstate_interfaces), which is what gives it the flexibility
# to support any bonding mode's real option set. The tradeoff is that a
# typo'd option name or wrong-typed value would otherwise only surface
# when nmstate itself rejects it at apply time. This table catches the
# common cases early instead, as a best-effort, NON-BLOCKING check:
#
#   - An option not in this table is passed through with a warning that
#     it's "unrecognized", not rejected - kernel bonding has ~30 possible
#     options and this table covers the commonly used/documented ones,
#     not all of them. A legitimate but obscure option shouldn't be
#     blocked just because it isn't listed here.
#   - A recognized option with the wrong type, or a value outside its
#     enum, gets a warning but is still passed through unchanged -
#     nmstate remains the final authority; this is an early heads-up,
#     not a gate.
#
# Which options are valid for a mode genuinely differs by mode (lacp_rate
# only makes sense for 802.3ad; primary only for active-backup/
# balance-tlb/balance-alb), so validation is keyed off the mode actually
# set in link_aggregation.mode.

KNOWN_BOND_MODES = {
    'balance-rr', 'active-backup', 'balance-xor', 'broadcast',
    '802.3ad', 'balance-tlb', 'balance-alb',
}

# Options valid for (almost) any mode. key -> expected Python type.
BOND_OPTIONS_COMMON = {
    'miimon': int,
    'updelay': int,
    'downdelay': int,
    'use_carrier': bool,
    'all_slaves_active': bool,
    'resend_igmp': int,
    'min_links': int,
    'packets_per_slave': int,
    'lp_interval': int,
}

# Options only valid for a specific mode. key -> expected type, or
# ('enum', {allowed strings}) for a fixed set of choices.
BOND_OPTIONS_BY_MODE = {
    '802.3ad': {
        'lacp_rate': ('enum', {'slow', 'fast'}),
        'lacp_active': bool,
        'ad_select': ('enum', {'stable', 'bandwidth', 'count'}),
        'xmit_hash_policy': ('enum', {
            'layer2', 'layer2+3', 'layer3+4', 'encap2+3', 'encap3+4', 'vlan+srcmac',
        }),
        'ad_actor_sys_prio': int,
        'ad_actor_system': str,
        'ad_user_port_key': int,
    },
    'active-backup': {
        'primary': str,
        'primary_reselect': ('enum', {'always', 'better', 'failure'}),
        'fail_over_mac': ('enum', {'none', 'active', 'follow'}),
        'num_grat_arp': int,
        'num_unsol_na': int,
    },
    'balance-tlb': {
        'primary': str,
        'primary_reselect': ('enum', {'always', 'better', 'failure'}),
        'tlb_dynamic_lb': bool,
    },
    'balance-alb': {
        'primary': str,
        'primary_reselect': ('enum', {'always', 'better', 'failure'}),
    },
    'balance-xor': {
        'xmit_hash_policy': ('enum', {
            'layer2', 'layer2+3', 'layer3+4', 'encap2+3', 'encap3+4', 'vlan+srcmac',
        }),
    },
    'balance-rr': {},
    'broadcast': {},
}


def _type_name(value):
    """Friendly type name for warning messages. Ansible wraps YAML-loaded
    strings/ints in its own internal subclasses (e.g. _AnsibleTaggedStr)
    for origin-tracking, so plain type(value).__name__ would show that
    internal class name instead of the plain type a person would expect
    to see (bool is checked before int since bool subclasses int)."""
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int'
    if isinstance(value, str):
        return 'str'
    return type(value).__name__


def validate_link_aggregation(mode, options):
    """Best-effort, non-blocking validation of link_aggregation options
    against the mode they're set for. Returns a list of warning strings.
    Never raises and never strips/modifies options - see module notes
    above for why this is deliberately a heads-up, not a gate."""
    warnings = []
    if mode not in KNOWN_BOND_MODES:
        warnings.append(
            "link_aggregation.mode %r is not one of the recognized nmstate "
            "bonding modes (%s) - its options were not validated against it"
            % (mode, ', '.join(sorted(KNOWN_BOND_MODES)))
        )
        return warnings

    mode_specific = BOND_OPTIONS_BY_MODE.get(mode, {})
    for key, value in options.items():
        spec = mode_specific.get(key, BOND_OPTIONS_COMMON.get(key))
        if spec is None:
            warnings.append(
                "link_aggregation option '%s' is not a recognized option for "
                "mode '%s' (or a common bonding option) - passed through "
                "unvalidated, double-check it's correct" % (key, mode)
            )
            continue
        if isinstance(spec, tuple) and spec[0] == 'enum':
            choices = spec[1]
            if value not in choices:
                warnings.append(
                    "link_aggregation.%s = %r is not one of %s for mode '%s'"
                    % (key, value, sorted(choices), mode)
                )
        else:
            expected_type = spec
            # bool is a subclass of int in Python - check it first so an
            # int value isn't silently accepted for a bool-typed option
            if expected_type is bool and not isinstance(value, bool):
                warnings.append(
                    "link_aggregation.%s = %r should be true/false, got %s"
                    % (key, value, _type_name(value))
                )
            elif expected_type is int and (isinstance(value, bool) or not isinstance(value, int)):
                warnings.append(
                    "link_aggregation.%s = %r should be an integer, got %s"
                    % (key, value, _type_name(value))
                )
            elif expected_type is str and not isinstance(value, str):
                warnings.append(
                    "link_aggregation.%s = %r should be a string, got %s"
                    % (key, value, _type_name(value))
                )
    return warnings


def _clean(d):
    """Drop keys whose value is None so we don't render 'key: null' for
    fields that simply weren't configured (e.g. mtu on a down interface)."""
    return {k: v for k, v in d.items() if v is not None}


def _bytes_ge(capacity_bytes, min_bytes):
    return capacity_bytes is not None and capacity_bytes >= min_bytes


# --------------------------------------------------------------------------
# rootDeviceHints
# --------------------------------------------------------------------------

def select_boot_disk(storage, min_bytes=DEFAULT_MIN_BOOT_BYTES):
    """Pick the first bootable disk >= min_bytes and the best available
    rootDeviceHints identifier for it.

    Prefers virtual_disks (what the OS actually sees when there's a HW
    RAID controller) and only looks at physical_drives when there are no
    virtual_disks - individual RAID member drives aren't visible to the
    OS as separate block devices, only the volume is.

    'bootable' itself comes from matching the drive/volume's Id or
    SerialNumber as a literal substring inside the BMC's BootOptions
    descriptions (see redfish_hw_facts.py's looks_bootable()) - this is a
    heuristic, and BootOptions text often doesn't literally contain
    either (confirmed on a real iDRAC9 with a single AHCI SATA disk,
    where BootOptions never matched). So: when there's exactly ONE
    size-eligible candidate disk and none passed the bootable check, use
    it anyway rather than reporting no boot disk found at all - there's
    no ambiguity about *which* disk when it's the only one, regardless of
    whether BootOptions could be matched textually. The returned note
    flags this so it's easy to grep for and spot-check.

    Hint preference order (returned as hint_key/hint_value, a single
    rootDeviceHints field - see rootDeviceHints() below for why only
    one):
      1. wwn - a hardware-intrinsic durable identifier (NAA/EUI/etc.),
         when the BMC's Redfish Identifiers array has one. This is
         EXACTLY what the installed OS itself reports for that device
         (udevadm/lsblk ID_WWN) - not a prediction, so it can't be wrong
         the way a by-path guess can be. Coverage varies by controller
         vendor, especially for RAID virtual Volumes - though a
         single-member RawDevice/pass-through volume (e.g. a plain AHCI
         disk, not real RAID) inherits its one physical drive's wwn/
         serial_number at discovery time (see discover_storage()), since
         it's literally the same disk represented twice.
      2. serialNumber - same reasoning, for physical Drives specifically
         (Redfish's Drive.SerialNumber is far more consistently
         populated across vendors than a Volume's Identifiers).
      3. predicted_by_path (deviceName) - deterministic given a correct
         PCI B:D:F (that's the whole point of by-path), but IS a
         prediction of how Linux will enumerate the SCSI target under
         that controller - wrong if the OEM PCI-location extraction
         (Oem.Dell.../Oem.Hpe...) doesn't quite match reality for this
         controller/firmware combo. Verify against a live boot
         (`ls -la /dev/disk/by-path/`) if by_path_confidence is
         'best_effort_pci_bdf_unknown', or as a general sanity check
         before trusting deviceName in production.
      4. '/dev/sda' (deviceName) - ONLY when it's the single candidate
         disk, so a guess can't be wrong about *which* disk. Flagged as
         best_effort.
      5. None - multiple candidates with no identifier of any kind:
         cannot safely guess. Surfaced as a warning for manual review.
    """
    storage = storage or {}
    candidates = storage.get('virtual_disks') or []
    pool = 'virtual_disks'
    if not candidates:
        candidates = [d for d in storage.get('physical_drives', [])
                      if not d.get('part_of_virtual_disk')]
        pool = 'physical_drives'

    size_ok = [d for d in candidates if _bytes_ge(d.get('capacity_bytes'), min_bytes)]
    matches = [d for d in size_ok if d.get('bootable')]

    assumed_bootable_note = None
    if not matches and len(size_ok) == 1:
        matches = size_ok
        assumed_bootable_note = (
            "This disk's 'bootable' flag was false (BootOptions text didn't "
            "match its Id/SerialNumber - a common false negative, not "
            "necessarily a real problem), but it's the ONLY disk >= "
            "%.0f GB in this system, so it was selected anyway. Verify "
            "before applying." % (min_bytes / 10**9)
        )

    if not matches:
        return {
            'hint_key': None,
            'hint_value': None,
            'source': pool,
            'id': None,
            'capacity_gb': None,
            'confidence': None,
            'note': 'NO MATCHING BOOT DISK FOUND (bootable + >= %.0f GB) - '
                    'manual rootDeviceHints required.' % (min_bytes / 10**9),
        }

    chosen = matches[0]
    ident = chosen.get('volume_id') or chosen.get('drive_id')
    capacity_gb = round((chosen.get('capacity_bytes') or 0) / (10 ** 9), 1)

    base = {'source': pool, 'id': ident, 'capacity_gb': capacity_gb,
            'note': assumed_bootable_note}

    if chosen.get('wwn'):
        return dict(base, hint_key='wwn', hint_value=chosen['wwn'], confidence='high')

    if chosen.get('serial_number'):
        return dict(base, hint_key='serialNumber', hint_value=chosen['serial_number'],
                     confidence='high')

    by_path = chosen.get('predicted_by_path')
    if by_path:
        return dict(base, hint_key='deviceName', hint_value=by_path,
                     confidence=chosen.get('by_path_confidence', 'unknown'))

    if len(candidates) == 1:
        note = ("No wwn/serialNumber/predicted_by_path available; guessed "
                "'/dev/sda' because it's the only candidate disk. Verify "
                "before applying.")
        if assumed_bootable_note:
            note = assumed_bootable_note + ' ' + note
        return dict(base, hint_key='deviceName', hint_value='/dev/sda',
                     confidence='best_effort_guess', note=note)

    return {
        'hint_key': None,
        'hint_value': None,
        'source': pool,
        'id': ident,
        'capacity_gb': capacity_gb,
        'confidence': None,
        'note': 'Multiple candidate disks and no predicted_by_path available - '
                'cannot safely guess kernel disk-letter ordering. MANUAL REVIEW '
                'REQUIRED before applying this AgentConfig.',
    }


# --------------------------------------------------------------------------
# top-level host.interfaces (name <-> MAC map)
# --------------------------------------------------------------------------

def flatten_ports(network_adapters, host_layout=None):
    """Every discovered port on every adapter, as {name, macAddress}.

    Ports with no predicted name or no MAC are silently skipped, UNLESS a
    manual 'name' override is given for that (adapter_id, port) in
    host_layout's members list - the same override build_nmstate_interfaces()
    uses (see resolve()'s docstring there), for cases like sushy-tools
    labs where Redfish has no PCI slot data to predict a name from at
    all. Keeping this consistent with build_nmstate_interfaces matters:
    without it, a sushy-tools host's networkConfig.interfaces would
    correctly show the overridden name while the top-level interfaces
    list (the installer's MAC-to-name map) stayed empty for the same
    port - an inconsistency within the same agent-config.yaml host entry.
    """
    host_layout = host_layout or {}
    overrides = {}
    for m in host_layout.get('members', []):
        if m.get('name'):
            overrides[(m.get('adapter_id'), m.get('port'))] = m['name']

    out = []
    for adapter in (network_adapters or []):
        for port in adapter.get('ports', []):
            key = (adapter.get('adapter_id'), port.get('port_number'))
            _assert_hashable_port_key(key, port)
            name = overrides.get(key) or port.get('predicted_linux_ifname')
            mac = port.get('mac_address')
            if not name or not mac:
                continue
            out.append({'name': name, 'macAddress': mac})
    return out


# --------------------------------------------------------------------------
# networkConfig.interfaces (nmstate)
# --------------------------------------------------------------------------

def _port_lookup(network_adapters):
    lookup = {}
    for adapter in (network_adapters or []):
        for port in adapter.get('ports', []):
            key = (adapter.get('adapter_id'), port.get('port_number'))
            _assert_hashable_port_key(key, port)
            lookup[key] = port
    return lookup


def build_nmstate_interfaces(network_adapters, host_layout, network_profile):
    """Build the full nmstate interfaces list for one host, from:
      - host_layout: this host's own {members, ip} (vars/network_layout.yaml)
      - network_profile: the CLUSTER-WIDE topology definition, same for
        every host (vars/cluster.yaml `network_profile:` - type, bond
        name, mode/mtu/LACP options, vlan_id, prefix_length)

    Emits:
      - type: ethernet -> one interface, optionally with a stacked vlan
      - type: bond     -> bond + member interfaces, optionally with a
                           stacked vlan instead of L3 directly on the bond
      - every discovered port NOT part of host_layout.members -> ethernet,
        state: down

    OpenShift's Agent-based Installer only supports a single active
    network at install time (ethernet, ethernet+vlan, bond, or
    bond+vlan) for the whole cluster - so network_profile.type/name/vlan
    are defined once and apply to every host; host_layout only supplies
    the per-host hardware mapping and IP that genuinely differ per server.

    IPv4 is always static (a single `ip` string in host_layout, combined
    with the cluster-wide `prefix_length`) - DHCP isn't supported here,
    since rendezvousIP requires a known address up front.

    Returns {'interfaces': [...], 'warnings': [...], 'primary_interface': ...}
    - warnings flag misconfiguration (wrong adapter_id/port, missing bond
    name, wrong member count for the type, no ip set) so they surface to
    the person running the playbook instead of failing silently.
    primary_interface is the name of whichever interface ends up carrying
    the host's IP (the bond/ethernet itself, or its vlan sub-interface) -
    used to build the default route.
    """
    host_layout = host_layout or {}
    network_profile = network_profile or {}
    lookup = _port_lookup(network_adapters)
    used_keys = set()
    warnings = []
    result = []
    primary_interface = None

    def resolve(adapter_id, port_num, context, name_override=None):
        """Look up a discovered port by (adapter_id, port_number). If the
        port has no predicted_linux_ifname (e.g. sushy-tools/KVM labs -
        see README - Redfish's EthernetInterfaces resource has no PCI
        slot data to predict a name from at all), name_override lets a
        person supply the real interface name themselves - something
        they can observe (virsh domiflist, or `ip link` on a booted
        guest) but this code has no way to derive from Redfish. This is
        NOT a guess: it only takes effect when the person has actually
        written a name in network_layout.yaml's member entry."""
        key = (adapter_id, port_num)
        port = lookup.get(key)
        if not port:
            warnings.append(
                '%s: adapter_id=%r port=%r not found in discovered network_adapters'
                % (context, adapter_id, port_num)
            )
            return None
        name = name_override or port.get('predicted_linux_ifname')
        if not name:
            warnings.append(
                "%s: adapter_id=%r port=%r has no predicted_linux_ifname and no "
                "manual 'name' override was given in network_layout.yaml - cannot use it"
                % (context, adapter_id, port_num)
            )
            return None
        used_keys.add(key)
        if name != port.get('predicted_linux_ifname'):
            resolved_port = dict(port)
            resolved_port['predicted_linux_ifname'] = name
            return resolved_port
        return port

    members_cfg = host_layout.get('members', [])
    ip = host_layout.get('ip')
    net_type = network_profile.get('type')
    base_name = None
    base_entry = None
    base_mac = None

    if not members_cfg:
        warnings.append(
            'No members defined for this host (missing network_layout.<host>.members) - '
            'every discovered port will be set to state: down and no default route can '
            'be created.'
        )
    elif net_type not in ('bond', 'ethernet'):
        warnings.append(
            "network_profile.type must be 'bond' or 'ethernet', got %r" % net_type
        )
    elif net_type == 'ethernet' and len(members_cfg) != 1:
        warnings.append(
            "network_profile.type is 'ethernet' but this host has %d members - "
            "ethernet supports exactly 1" % len(members_cfg)
        )
    elif net_type == 'bond' and len(members_cfg) < 2:
        warnings.append(
            "network_profile.type is 'bond' but this host only has %d member(s) - "
            "need at least 2" % len(members_cfg)
        )
    else:
        resolved = []
        for m in members_cfg:
            port = resolve(m.get('adapter_id'), m.get('port'), 'network member', name_override=m.get('name'))
            if port:
                resolved.append(port)

        if net_type == 'bond':
            bond_name = network_profile.get('name')
            if not bond_name:
                warnings.append("network_profile.type is 'bond' but no 'name' was given (e.g. bond0)")
            member_names = []
            # The bond adopts its first (primary) member's MAC as the
            # address it actually presents to the network - nmstate/
            # NetworkManager reflects this by setting 'mac-address' to
            # that shared value on every member, the bond itself, and any
            # vlan stacked on top. Each member's OWN real hardware MAC is
            # preserved separately as 'permanent-mac-address', which only
            # applies to ethernet-type (bond member) interfaces - not to
            # the bond or vlan entries themselves.
            shared_mac = resolved[0].get('mac_address') if resolved else None
            for port in resolved:
                name = port['predicted_linux_ifname']
                own_mac = port.get('mac_address')
                result.append(_clean({
                    'name': name,
                    'type': 'ethernet',
                    'state': 'up',
                    'mtu': network_profile.get('mtu'),
                    'mac-address': shared_mac,
                    'permanent-mac-address': own_mac,
                }))
                member_names.append(name)
            if bond_name:
                # link_aggregation is passed through as-is: pull 'mode'
                # out for nmstate's link-aggregation.mode, and forward
                # every other key verbatim into link-aggregation.options.
                # Nothing here is hardcoded to 802.3ad's option set, so
                # any mode's real nmstate options work (e.g. active-backup
                # + primary) without touching this filter.
                link_agg_cfg = network_profile.get('link_aggregation', {}) or {}
                mode = link_agg_cfg.get('mode', '802.3ad')
                options = {k: v for k, v in link_agg_cfg.items() if k != 'mode'}
                warnings.extend(validate_link_aggregation(mode, options))
                link_aggregation = {'mode': mode, 'port': member_names}
                if options:
                    link_aggregation['options'] = options
                base_entry = {
                    'name': bond_name,
                    'type': 'bond',
                    'state': 'up',
                    'mtu': network_profile.get('mtu'),
                    'mac-address': shared_mac,
                    'link-aggregation': link_aggregation,
                }
                base_name = bond_name
                base_mac = shared_mac

        else:  # ethernet
            port = resolved[0] if resolved else None
            if port:
                own_mac = port.get('mac_address')
                base_entry = {
                    'name': port['predicted_linux_ifname'],
                    'type': 'ethernet',
                    'state': 'up',
                    'mtu': network_profile.get('mtu'),
                    'mac-address': own_mac,
                    # No permanent-mac-address here: it only applies to
                    # ethernet interfaces that are bond members, where it
                    # distinguishes a member's own MAC from the MAC the
                    # bond presents. A standalone (non-bonded) ethernet
                    # interface only ever has the one MAC.
                }
                base_name = port['predicted_linux_ifname']
                base_mac = own_mac

    if base_name and base_entry is not None:
        if not ip:
            warnings.append(
                "No static 'ip' set for this host (network_layout.<host>.ip) - "
                "cannot build an ipv4 config or a default route for it."
            )
            ipv4 = {'enabled': False}
        else:
            ipv4 = {
                'enabled': True,
                'dhcp': False,
                'address': [{'ip': ip, 'prefix-length': network_profile.get('prefix_length')}],
            }

        vlan_id = network_profile.get('vlan_id')
        if vlan_id:
            base_entry['ipv4'] = {'enabled': False}  # base carries no L3
            result.append(_clean(base_entry))
            vlan_name = '%s.%s' % (base_name, vlan_id)
            result.append(_clean({
                'name': vlan_name,
                'type': 'vlan',
                'state': 'up',
                # The vlan sub-interface also carries the same shared MAC
                # as its base (bond or ethernet) - it's a virtual overlay
                # on the same physical link, not a separate address.
                'mac-address': base_mac,
                'vlan': {'base-iface': base_name, 'id': vlan_id},
                'ipv4': ipv4,
            }))
            if ip:
                primary_interface = vlan_name
        else:
            base_entry['ipv4'] = ipv4
            result.append(_clean(base_entry))
            if ip:
                primary_interface = base_name

    # --- everything else discovered but not part of the network -> down --
    for key, port in lookup.items():
        if key in used_keys:
            continue
        name = port.get('predicted_linux_ifname')
        if not name:
            warnings.append(
                "unused port adapter_id=%r port=%r has no predicted_linux_ifname; "
                "omitted from networkConfig (real BMC hardware only - this shouldn't "
                "happen against physical iDRAC/iLO/XCC data)" % (key[0], key[1])
            )
            continue
        result.append({'name': name, 'type': 'ethernet', 'state': 'down'})

    return {'interfaces': result, 'warnings': warnings, 'primary_interface': primary_interface}


# --------------------------------------------------------------------------
# dns-resolver / routes - CLUSTER-WIDE, not per host. Sourced from
# vars/cluster.yaml (dns, gateway, route_table_id), not network_layout.yaml.
# --------------------------------------------------------------------------

def dns_resolver(dns_cfg):
    """dns_cfg is the cluster-wide vars/cluster.yaml `dns:` block
    ({server: [...], search: [...]}) - same for every host."""
    if not dns_cfg:
        return None
    return {'config': {'server': dns_cfg.get('server', []), 'search': dns_cfg.get('search', [])}}


def routes_config(gateway, primary_interface, table_id=254):
    """Builds the single default-route entry every host gets:
    destination 0.0.0.0/0 via the cluster-wide `gateway`, over whichever
    interface build_nmstate_interfaces() determined is this host's one
    IP-carrying interface (ethernet, bond, or a vlan stacked on either -
    OpenShift's Agent-based Installer only supports one network at
    install, so there's exactly one candidate).

    Returns None (omit routes entirely) when either piece is missing,
    rather than emitting a route to nowhere - primary_interface is None
    when build_nmstate_interfaces() couldn't find exactly one IP-enabled
    interface, and that's already surfaced as a warning by the caller.
    """
    if not gateway or not primary_interface:
        return None
    return {'config': [_clean({
        'destination': '0.0.0.0/0',
        'next-hop-address': gateway,
        'next-hop-interface': primary_interface,
        'table-id': table_id,
    })]}


# --------------------------------------------------------------------------
# install-config.yaml support: CIDR membership checks
# --------------------------------------------------------------------------

def ip_in_network(ip, cidr):
    """True if ip falls within cidr (e.g. '10.10.10.11' in '10.10.10.0/24').
    Used by generate-agent-config.yaml to validate that every node's IP,
    and every apiVIPs/ingressVIPs entry, actually belongs to
    install-config.yaml's machineNetwork - install-config.yaml itself
    won't catch a mismatch like this; the Assisted/Agent-based Installer
    would only fail much later, during actual cluster bring-up. Returns
    False (never raises) for malformed input, so the caller gets a clean
    Ansible assert failure naming the bad value instead of a Python
    traceback with no context."""
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return False


class FilterModule(object):
    def filters(self):
        return {
            'select_boot_disk': select_boot_disk,
            'flatten_ports': flatten_ports,
            'build_nmstate_interfaces': build_nmstate_interfaces,
            'dns_resolver': dns_resolver,
            'routes_config': routes_config,
            'ip_in_network': ip_in_network,
        }
