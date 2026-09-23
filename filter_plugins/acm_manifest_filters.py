# -*- coding: utf-8 -*-
"""
Ansible filter plugin used by generate-acm-manifests.yaml.

Complements filter_plugins/agent_config_filters.py (which builds
agent-config.yaml/install-config.yaml) with the two pieces that are
specific to per-node ACM/Assisted-Installer CRs (NMStateConfig,
BareMetalHost) and aren't already covered there:

  member_interfaces  -> NMStateConfig's top-level spec.interfaces
                         MAC<->name map (only the configured members,
                         NOT every discovered port - see docstring)
  boot_mac_address    -> BareMetalHost spec.bootMACAddress
  bmc_redfish_address -> BareMetalHost spec.bmc.address

build_nmstate_interfaces/dns_resolver/routes_config from
agent_config_filters.py are reused as-is for NMStateConfig's
spec.config - that structure is identical to agent-config.yaml's
networkConfig.
"""


def _port_lookup(network_adapters):
    lookup = {}
    for adapter in (network_adapters or []):
        for port in adapter.get('ports', []):
            key = (adapter.get('adapter_id'), port.get('port_number'))
            # Defense in depth: redfish_hw_facts.py is now careful to
            # never produce a non-scalar port_number (confirmed needed
            # on real Dell iDRAC8 hardware, where the BMC itself
            # returned a raw @odata.id reference object instead of an
            # integer for PhysicalPortNumber), but a genuinely unknown
            # vendor/firmware quirk could still slip an unhashable value
            # like a dict or list through some day. Fail with a clear,
            # actionable message pointing at exactly which port -
            # instead of a bare "unhashable type: 'dict'" with no
            # indication of where it came from.
            try:
                hash(key)
            except TypeError:
                raise ValueError(
                    "port_number for adapter '%s' port '%s' is not a plain "
                    "value (got %r) - check hw_inventory.yaml directly; this "
                    "usually means the BMC returned something unexpected for "
                    "this port's Redfish PhysicalPortNumber/Id and needs a "
                    "targeted fix in library/redfish_hw_facts.py, not here."
                    % (adapter.get('adapter_id'), port.get('port_number'), port.get('port_number'))
                )
            lookup[key] = port
    return lookup


def member_interfaces(network_adapters, host_layout):
    """NMStateConfig's top-level spec.interfaces: only the ports actually
    configured for this host (network_layout.<host>.members), in order -
    NOT every discovered port. This is deliberately narrower than
    agent_config_filters.flatten_ports(), which lists every discovered
    port (including unused ones, for agent-config.yaml's installer-facing
    interfaces list) - NMStateConfig's top-level interfaces block is just
    the MAC<->name mapping nmstate needs for the interfaces it's actually
    told to configure in spec.config.
    """
    host_layout = host_layout or {}
    lookup = _port_lookup(network_adapters)
    out = []
    for m in host_layout.get('members', []):
        port = lookup.get((m.get('adapter_id'), m.get('port')))
        if not port:
            continue
        name = m.get('name') or port.get('predicted_linux_ifname')
        mac = port.get('mac_address')
        if not name or not mac:
            continue
        out.append({'name': name, 'macAddress': mac})
    return out


def boot_mac_address(network_adapters, host_layout):
    """BareMetalHost spec.bootMACAddress: the MAC of the FIRST configured
    member in network_layout.<host>.members - the same port
    build_nmstate_interfaces() treats as the base/primary interface for a
    bond, or the only interface for standalone ethernet. This is the NIC
    the virtual-media boot ISO actually attaches to.

    Returns None (surfaced by the playbook as a hard failure - unlike the
    nmstate warnings, a missing bootMACAddress makes the BareMetalHost
    unusable) if there are no configured members or the first one doesn't
    resolve against discovered hardware.
    """
    host_layout = host_layout or {}
    members = host_layout.get('members', [])
    if not members:
        return None
    first = members[0]
    port = _port_lookup(network_adapters).get((first.get('adapter_id'), first.get('port')))
    return port.get('mac_address') if port else None


def bmc_redfish_address(vendor, bmc_address, virtualbmc=False, virtual_domain=None,
                         bmc_port=None, system_id=None):
    """Builds BareMetalHost spec.bmc.address.

    - virtualbmc (sushy-tools/KVM lab): redfish-virtualmedia, sushy-tools'
      shared emulator host:port (bmc_address here IS the shared sushy-tools
      host, e.g. inventory's bmc_ip_hostname), System resource keyed by the
      libvirt domain name - same virtual_domain used for discovery, see
      library/redfish_hw_facts.py and README's sushy-tools section.
    - real hardware, Dell ('Dell' appears in the discovered vendor string,
      e.g. 'Dell Inc.'): idrac-virtualmedia, System.Embedded.1 - Dell's
      standard, single-system-per-BMC Redfish System ID.
    - real hardware, any other vendor: redfish-virtualmedia with an
      explicit system_id if given, else 'System.Embedded.1' as a
      best-effort default. NOT verified against real HPE iLO / Lenovo XCC
      / generic Redfish hardware - override with a per-host
      redfish_system_id in inventory/hosts.yaml if your BMC's Redfish
      System ID differs (GET /redfish/v1/Systems on that BMC to confirm).
    """
    system_id = system_id or 'System.Embedded.1'
    if virtualbmc:
        port = bmc_port or 8000
        return 'redfish-virtualmedia://%s:%s/redfish/v1/Systems/%s' % (
            bmc_address, port, virtual_domain,
        )
    scheme = 'idrac-virtualmedia' if 'dell' in (vendor or '').lower() else 'redfish-virtualmedia'
    return '%s://%s/redfish/v1/Systems/%s' % (scheme, bmc_address, system_id)


class FilterModule(object):
    def filters(self):
        return {
            'member_interfaces': member_interfaces,
            'boot_mac_address': boot_mac_address,
            'bmc_redfish_address': bmc_redfish_address,
        }
