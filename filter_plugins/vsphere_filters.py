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


def vsphere_static_hosts(bootstrap, masters, workers, hostvars, gateway, nameservers, prefix_length):
    """platform.vsphere.hosts: the bootstrap, then masters and workers in
    inventory order. bootstrap: {'ip', 'vsphere_failure_domain'}; each node
    gives its `ip` and optional `vsphere_failure_domain` host var. Gateway
    and nameservers are the same for every node (OpenShift puts every node
    on the one machine network)."""
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

    hosts = [entry('bootstrap', bootstrap['ip'], bootstrap.get('vsphere_failure_domain'))]
    for role, names in (('control-plane', masters), ('compute', workers)):
        for name in names:
            hosts.append(entry(role, hostvars[name]['ip'], hostvars[name].get('vsphere_failure_domain')))
    return hosts


def vsphere_failure_domain(fd, vcenter):
    """fd keys: name, region, zone, datacenter, compute_cluster, datastore,
    networks, optional resource_pool (default <compute_cluster>/Resources),
    optional folder, optional server (default the vsphere.vcenter)."""
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
        'server': fd.get('server') or vcenter,
        'topology': topology,
        'zone': fd['zone'],
    }


class FilterModule(object):
    def filters(self):
        return {
            'vsphere_static_hosts': vsphere_static_hosts,
            'vsphere_failure_domain': vsphere_failure_domain,
        }
