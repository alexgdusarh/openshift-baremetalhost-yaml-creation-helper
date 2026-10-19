# -*- coding: utf-8 -*-
"""
Ansible filter plugin used by generate-vsphere-ipi-config.yaml.

Turns this repo's cluster data into the two list entries of a vSphere IPI
install-config.yaml that differ per item:

  vsphere_static_hosts    -> platform.vsphere.hosts[] (role + static
                             networkDevice, optional failureDomain each)
  vsphere_failure_domain  -> one platform.vsphere.failureDomains[] entry
                             from a vsphere.failure_domains item of
                             vars/cluster.yaml (snake_case keys)
"""


def vsphere_static_hosts(bootstrap, masters, workers, hostvars, gateway, nameservers, prefix_length,
                         failure_domains):
    """platform.vsphere.hosts: the bootstrap, then masters and workers in
    inventory order. bootstrap: {'ip', 'vsphere_failure_domain'}; each node
    gives its `ip` and optional `vsphere_failure_domain` host var. Gateway
    and nameservers are the same for every node (OpenShift puts every node
    on the one machine network).

    failure_domains: the failure domain names, 1 to n. With one, entries
    carry no failureDomain (the installer uses it). With several, every
    entry gets one: its own setting, else round-robin over the list within
    its role (master-0 -> first, master-1 -> second, ...), so the masters
    and the workers spread across all domains; the bootstrap defaults to
    the first."""
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
                   bootstrap.get('vsphere_failure_domain') or (failure_domains[0] if spread else None))]
    for role, names in (('control-plane', masters), ('compute', workers)):
        for index, name in enumerate(names):
            own = hostvars[name].get('vsphere_failure_domain')
            hosts.append(entry(role, hostvars[name]['ip'],
                               own or (failure_domains[index % len(failure_domains)] if spread else None)))
    return hosts


def vsphere_failure_domain(fd, vcenter):
    """fd keys: name, region, zone, datacenter, compute_cluster, datastore,
    networks, optional resource_pool (default <compute_cluster>/Resources),
    optional folder."""
    topology = {
        'computeCluster': fd['compute_cluster'],
        'datacenter': fd['datacenter'],
        'datastore': fd['datastore'],
        'networks': list(fd['networks']),
        'resourcePool': fd.get('resource_pool') or fd['compute_cluster'] + '/Resources',
    }
    if fd.get('folder'):
        topology['folder'] = fd['folder']
    return {
        'name': fd['name'],
        'region': fd['region'],
        'server': vcenter,
        'topology': topology,
        'zone': fd['zone'],
    }


class FilterModule(object):
    def filters(self):
        return {
            'vsphere_static_hosts': vsphere_static_hosts,
            'vsphere_failure_domain': vsphere_failure_domain,
        }
