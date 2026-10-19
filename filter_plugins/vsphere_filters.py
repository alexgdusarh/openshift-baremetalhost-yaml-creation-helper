# -*- coding: utf-8 -*-
"""
Ansible filter plugin used by generate-vsphere-ipi-fix-ips-config.yaml.

Turns the vsphere: block of vars/cluster.yaml (snake_case keys) and the
inventory into the lists of a vSphere IPI install-config.yaml:

  vsphere_failure_domain_servers -> vsphere.failure_domains with each
                                    entry's vCenter `server` filled in
                                    when there is a single vCenter
  vsphere_static_hosts           -> platform.vsphere.hosts[] (role + static
                                    networkDevice, optional failureDomain)
  vsphere_machine_pool           -> controlPlane/compute `platform:` from
                                    vsphere.control_plane / vsphere.compute
  vsphere_failure_domain         -> one platform.vsphere.failureDomains[]
  vsphere_vcenters               -> platform.vsphere.vcenters[], each with
                                    the datacenters its failure domains use
"""


def vsphere_failure_domain_servers(failure_domains, vcenters):
    """With exactly one vCenter, a failure domain may leave out `server`;
    fill it in. With several, every failure domain must name its own (the
    playbook's checks report the ones that don't)."""
    default = vcenters[0].get('server') if len(vcenters) == 1 else None
    resolved = []
    for fd in failure_domains:
        fd = dict(fd)
        if not fd.get('server') and default:
            fd['server'] = default
        resolved.append(fd)
    return resolved


def vsphere_static_hosts(bootstrap, masters, workers, hostvars, gateway, nameservers, prefix_length,
                         failure_domains, control_plane_zones, compute_zones):
    """platform.vsphere.hosts: the bootstrap, then masters and workers in
    inventory order. bootstrap: {'ip', 'vsphere_failure_domain'}; each node
    gives its `ip` and optional `vsphere_failure_domain` host var. Gateway
    and nameservers are the same for every node (OpenShift puts every node
    on the one machine network).

    failure_domains: the failure domain names, 1 to n. With one, entries
    carry no failureDomain (the installer uses it). With several, every
    entry gets one: its own setting, else round-robin over its machine
    pool's zones (master-0 -> first, master-1 -> second, ...), the order
    the installer spreads that pool's machines in; the bootstrap defaults
    to the first control-plane zone."""
    def entry(role, ip, failure_domain):
        host = {'role': role}
        if failure_domain:
            host['failureDomain'] = failure_domain
        host['networkDevice'] = {
            'ipAddrs': ['%s/%s' % (ip, prefix_length)],
            'gateway': gateway,
            'nameservers': list(nameservers),
        }
        return host

    spread = len(failure_domains) > 1
    hosts = [entry('bootstrap', bootstrap['ip'],
                   bootstrap.get('vsphere_failure_domain') or (control_plane_zones[0] if spread else None))]
    for role, names, zones in (('control-plane', masters, control_plane_zones), ('compute', workers, compute_zones)):
        for index, name in enumerate(names):
            own = hostvars[name].get('vsphere_failure_domain')
            hosts.append(entry(role, hostvars[name]['ip'],
                               own or (zones[index % len(zones)] if spread else None)))
    return hosts


def vsphere_machine_pool(pool):
    """A machine pool's `platform:` from vsphere.control_plane or
    vsphere.compute: zones, cpus, cores_per_socket, memory_mb, os_disk_gb,
    each written only when set, so the installer's own defaults apply to
    the rest ({} when nothing is set)."""
    pool = pool or {}
    vsphere = {}
    if pool.get('zones'):
        vsphere['zones'] = list(pool['zones'])
    for key, field in (('cpus', 'cpus'), ('cores_per_socket', 'coresPerSocket'), ('memory_mb', 'memoryMB')):
        if pool.get(key) is not None:
            vsphere[field] = int(pool[key])
    if pool.get('os_disk_gb') is not None:
        vsphere['osDisk'] = {'diskSizeGB': int(pool['os_disk_gb'])}
    return {'vsphere': vsphere} if vsphere else {}


def vsphere_failure_domain(fd):
    """fd keys: name, region, zone, server (its vCenter), datacenter,
    compute_cluster, datastore, networks, optional resource_pool and
    folder (written only when set; the installer defaults them)."""
    topology = {
        'computeCluster': fd['compute_cluster'],
        'datacenter': fd['datacenter'],
        'datastore': fd['datastore'],
        'networks': list(fd['networks']),
    }
    if fd.get('resource_pool'):
        topology['resourcePool'] = fd['resource_pool']
    if fd.get('folder'):
        topology['folder'] = fd['folder']
    return {
        'name': fd['name'],
        'region': fd['region'],
        'server': fd['server'],
        'topology': topology,
        'zone': fd['zone'],
    }


def vsphere_vcenters(vcenters, failure_domains):
    """vcenters items: server, username, password, optional port (443).
    datacenters: every datacenter a failure domain on that vCenter uses,
    in first-use order."""
    result = []
    for vc in vcenters:
        datacenters = []
        for fd in failure_domains:
            if fd.get('server') == vc['server'] and fd['datacenter'] not in datacenters:
                datacenters.append(fd['datacenter'])
        result.append({
            'datacenters': datacenters,
            'password': vc['password'],
            'port': int(vc.get('port') or 443),
            'server': vc['server'],
            'user': vc['username'],
        })
    return result


class FilterModule(object):
    def filters(self):
        return {
            'vsphere_failure_domain_servers': vsphere_failure_domain_servers,
            'vsphere_vcenters': vsphere_vcenters,
            'vsphere_static_hosts': vsphere_static_hosts,
            'vsphere_machine_pool': vsphere_machine_pool,
            'vsphere_failure_domain': vsphere_failure_domain,
        }
