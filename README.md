# redfish-hw-inventory

Vendor-neutral Ansible tooling for OpenShift bare-metal bring-up:
discovers NIC/storage hardware from a list of BMCs (iDRAC, iLO, Lenovo
XCC, Supermicro, or any DMTF Redfish 1.x compliant BMC, plus sushy-tools
labs), then renders an Agent-based Installer `agent-config.yaml` from
that discovery. Structured to manage many separate OpenShift clusters
(SNO through 5 masters + any number of workers) from one repo — see
"Managing multiple clusters" below.

**Stage 1** (`playbook.yaml`) builds `<cluster>/output/hw_inventory.yaml` —
one dict keyed by inventory hostname, each entry holding:

- `network_adapters` — every NIC, its PCI slot, port count, MAC per port,
  and a **predicted** Linux interface name (`eno1`, `ens2f1`, ...)
- `storage` — every storage controller, physical drive, and virtual disk,
  with a **predicted** `/dev/disk/by-path/...` and a `bootable` flag

```yaml
name-of-the-host:
  bmc_address: 10.10.10.11
  model: PowerEdge R750
  vendor: Dell Inc.
  network_adapters: [...]
  storage: {...}
```

**Stage 2** (`generate-agent-config.yaml`) turns that discovery plus your
own cluster/network config into `<cluster>/output/agent-config.yaml` —
see "Generating agent-config.yaml" further down.

See `clusters/ocp-lab/output/hw_inventory.example.yaml` for a full worked
example.

## How it works

There's a single custom module, `library/redfish_hw_facts.py`. It talks
straight to `https://<bmc>/redfish/v1/...` with basic auth — no Dell/HPE
Python SDK, no `dellemc.openmanage` / `community.general` dependency. It
walks:

- `/redfish/v1/Chassis/{id}/NetworkAdapters` → `Ports` (or the older
  `NetworkDeviceFunctions`/`NetworkPorts` split schema) for NIC/port/MAC data
- `/redfish/v1/Systems/{id}/Storage/{id}` → `StorageControllers`, `Drives`,
  `Volumes` for disk data
- `/redfish/v1/Systems/{id}/BootOptions` to flag which volume/drive is in
  the current UEFI boot order

The playbook (`playbook.yaml`) has two plays: the first loops over the
`bmc_hosts` inventory group and calls the module against each BMC
(`connection: local`, since we're talking HTTP to an out-of-band IP, not
SSHing anywhere); the second aggregates every host's results into two
dicts and writes them out with `to_nice_yaml`.

## Requirements

- Ansible core (any reasonably recent 2.1x)
- Network access from the control node to every BMC's HTTPS management port
- A Redfish-capable BMC. iDRAC7+ / iLO4+ / XCC all qualify.

No extra Python packages needed — the module uses Ansible's own
`fetch_url`, not `requests`.

## Setup

1. Copy `clusters/ocp-lab/` to `clusters/<your-cluster-name>/` as a
   starting point (or start from scratch — see "Managing multiple
   clusters" below for the exact layout each cluster directory needs).
2. Edit `clusters/<name>/inventory/hosts.yaml` — one entry per BMC,
   `bmc_ip_hostname` = the BMC's own IP (not the host OS IP). Named
   `bmc_ip_hostname` rather than the usual `ansible_host` deliberately —
   every play here runs with `connection: local`, so `ansible_host` would
   never actually be used to connect to anything, and naming it
   explicitly makes clear this value *is* the BMC endpoint.
3. Edit `clusters/<name>/inventory/hosts.yaml`'s `bmc_hosts.vars` block —
   set `bmc_username`/`bmc_password` there for the shared/default case, or
   under an individual host entry instead if that server has its own
   credentials (host-level values always override the group default; see
   the comments in `clusters/ocp-lab/inventory/hosts.yaml`, including a
   worked per-host-override example). For real use, vault the password:
   ```
   ansible-vault encrypt_string 'RootPassw0rd' --name 'bmc_password' \
     >> clusters/<name>/inventory/hosts.yaml
   ```
   (paste the resulting `!vault` block in place of the plain string,
   under `vars:` or under the specific host, same as above)
4. Run it:
   ```
   ./run-cluster.sh <name> discover -- --ask-vault-pass
   ```
5. Check `clusters/<name>/output/hw_inventory.yaml`.

A filled-in example of the expected shape is already in
`clusters/ocp-lab/output/hw_inventory.example.yaml`.

## Managing multiple clusters

Each cluster gets its own self-contained directory under `clusters/`:

```
clusters/<cluster-name>/
├── inventory/hosts.yaml       # this cluster's BMCs, credentials, per-host network_members/ip, masters/workers groups
├── vars/cluster.yaml          # this cluster's name/rendezvous IP/DNS/gateway/network_profile
└── output/
    ├── hw_inventory.yaml      # written by playbook.yaml
    └── agent-config.yaml     # written by generate-agent-config.yaml
```

Both `playbook.yaml` and `generate-agent-config.yaml` are completely
cluster-agnostic — all cluster-specific data lives in that directory, not
in the playbook/module/filter code. That's a deliberate split: fixing a
bug in the discovery logic or the nmstate builder fixes it for every
cluster at once, instead of needing to be patched into N forked copies
of the repo.

Run everything through **`run-cluster.sh`**, which derives both `-i` and
`cluster_dir` from a single cluster-name argument (they're always the
same `clusters/<name>/` path, so passing both separately would just be
two ways of saying the same thing):

```
./run-cluster.sh <cluster-name> discover        # stage 1: hw_inventory.yaml
./run-cluster.sh <cluster-name> agent-config     # stage 2: agent-config.yaml
./run-cluster.sh <cluster-name> all              # both, in order

# anything after -- is passed straight through to ansible-playbook:
./run-cluster.sh <cluster-name> discover -- --ask-vault-pass
./run-cluster.sh <cluster-name> discover -- -e bmc_password=...
```

Calling either playbook directly works too (both require `-i` and
`cluster_dir` explicitly — there's no default inventory in `ansible.cfg`
on purpose, so a forgotten `-i` fails loudly instead of silently
targeting the wrong cluster):

```
ansible-playbook playbook.yaml \
  -i clusters/<cluster-name>/inventory/hosts.yaml \
  -e cluster_dir=clusters/<cluster-name>
```

Two ready-to-use examples ship in `clusters/`:

| Directory | What it is |
|---|---|
| `clusters/ocp-lab/` | Real hardware (iDRAC/iLO/XCC/etc.), 3 masters + 1 worker, bonded network |
| `clusters/ocp-lab-kvm/` | sushy-tools/KVM lab, 3 masters + 1 worker, standalone ethernet (no bond) |

**Master count**: OpenShift supports 1 (SNO), 2, 3, 4, or 5 masters —
nothing in these playbooks assumes exactly 3. `role:` comes from
inventory group membership (`masters`/`workers`), and every check
(`rendezvous_ip` matching a master, boot-disk selection, nmstate
building) operates per-host regardless of how many hosts are in either
group. An SNO cluster is just an inventory with one host under `masters`
and an empty (or omitted) `workers` group — see the layout above, nothing
else changes.

**Hostnames only need to be unique within their own cluster's inventory**
— `server01-bmc` reused across two different `clusters/<name>/` is fine,
since each run loads exactly one inventory and writes to exactly one
cluster's `output/`.


## Lab use: sushy-tools virtual BMC on KVM/libvirt

Validated against the [sushy-tools dynamic emulator docs](https://docs.openstack.org/sushy-tools/latest/user/dynamic-emulator.html)
and tested against a mock server shaped to match. Two things about
sushy-tools' libvirt driver differ from a real BMC, and the module
handles both:

1. **One Redfish endpoint serves every VM** — there's a single BMC
   IP/port for the whole hypervisor, not one per server.
2. **The System resource is keyed by libvirt domain name** —
   `/redfish/v1/Systems/<domain>` (e.g. `.../Systems/vbmc-node`) rather
   than a numeric ID reached by walking the `Systems` collection.

| Param | Purpose |
|---|---|
| `virtualbmc: true` | Fetch `/redfish/v1/Systems/<virtual_domain>` directly instead of walking the `Systems` collection |
| `virtual_domain: <name>` | The libvirt domain name, exactly as shown by `virsh list --all` (case-sensitive). Required when `virtualbmc: true`. |
| `use_ssl: false` | sushy-tools defaults to plain HTTP |
| `port: 8000` | sushy-tools' default emulator port |

See `clusters/ocp-lab-kvm/` for a ready-to-edit example (its
`inventory/hosts.yaml` `bmc_hosts.vars` block already has the right
`bmc_username`/`bmc_use_ssl`/`bmc_port` for a default sushy-tools setup,
since each cluster directory is fully self-contained — no `-e` overrides
needed just to point at a lab):

```
./run-cluster.sh ocp-lab-kvm discover
```

### What's genuinely different in a sushy-tools lab (not just "sparse data")

**Chassis is NOT per-VM.** The docs are explicit that Chassis in
sushy-tools is a single shared, statically-configured (or auto-generated)
resource that every System "pretends to reside in" — it has its own UUID
unrelated to any VM's domain name. An earlier version of this module
tried `/redfish/v1/Chassis/<virtual_domain>`, which is wrong and 404s.
The module no longer does this in `virtualbmc` mode.

**NetworkAdapters (the PCI-slot/port/MAC-per-port resource this module
uses against real hardware) isn't implemented by sushy-tools at all.**
Per its feature-set docs, sushy-tools instead emulates the older, flatter
`EthernetInterfaces` resource under `Systems/<domain>/EthernetInterfaces`
— one MAC per virtual NIC, with no PCI slot, no port grouping, no card
concept. So in `virtualbmc` mode, `discover_network()` queries
`EthernetInterfaces` instead and returns each interface with
`source: "EthernetInterfaces"`, `pci_slot: null`, and
`predicted_linux_ifname: null` — there's genuinely no PCI location data
in sushy-tools to predict a name from, not a gap in this module's logic.
Requires the emulator's feature set to be `vmedia` or `full` (the
`minimum` feature set has no network resource at all).

Because `predicted_linux_ifname` is always `null` there, generating
`agent-config.yaml` for a sushy-tools host needs one extra piece: each
host's `network_members` entries in `inventory/hosts.yaml` accept an
optional manual `name:` field (e.g. `{adapter_id: eth0, port: 1, name:
enp1s0}`) — this code has no way to derive the real interface name from
Redfish here, but you can observe it (`virsh domiflist <domain>`, or `ip
link`/`nmcli` on a booted guest) and supply it directly.
`clusters/ocp-lab-kvm/inventory/hosts.yaml` already does this. Without
it, that host's member fails to resolve and you'll see a warning in
`agent_config_warnings` rather than a silently wrong interface name.

**Storage is a static, hand-authored dataset, not auto-discovered from
the VM's real disks.** The docs show `Storage`/`Drives`/`Volumes` are
populated entirely from `SUSHY_EMULATOR_STORAGE` / `SUSHY_EMULATOR_DRIVES`
/ `SUSHY_EMULATOR_VOLUMES` config on the sushy-tools side (keyed by the
System's ID, i.e. the domain name for the libvirt driver) — sushy-tools
does not introspect the VM's actual attached libvirt volumes into this
resource. If you haven't configured that, `storage.controllers` /
`physical_drives` / `virtual_disks` will correctly come back empty; that's
expected, not a bug. Also note the example `Drive` objects in the docs
have no `PhysicalLocation` field, so this module's `scsi_target` falls
back to a regex on the drive `Id`'s trailing digits, which won't always
resolve cleanly for sushy-tools' free-form drive IDs (e.g.
`32ADF365C6C1B7BD` has no trailing digit) — expect `by_path_confidence`
to be `best_effort_pci_bdf_unknown` for lab data.

In short: this module's NIC/disk logic is written and validated against
the real DMTF Redfish schema for physical iDRAC/iLO/XCC hardware. Against
a sushy-tools lab you'll correctly get power/boot control and a bare MAC
address per virtual NIC, but not PCI-slot-aware NIC/RAID modeling —
that's a real capability gap in the emulator, not something to debug in
this module. The `name:` override above is what makes full
`agent-config.yaml` generation (not just discovery) still work despite
that gap.

## Linux interface naming algorithm

This reproduces systemd's predictable-network-interface-names scheme:

| Redfish signal | Predicted name |
|---|---|
| `Location.PartLocation.ServiceLabel` contains "Integrated"/"Embedded"/"LOM"/"Onboard" | `eno<N>` — N counts sequentially across all onboard ports found |
| `Location.PartLocation.LocationOrdinalValue` = slot number, port/function 0 | `ens<slot>` |
| Same, function > 0 (multi-port add-in card) | `ens<slot>f<function>` |
| No location data exposed by firmware | `predicted_linux_ifname: null` |

This is accurate whenever the firmware correctly reports ACPI `_SUN` /
SMBIOS slot data to the BMC — which Dell and HPE do consistently. Some
whitebox/Supermicro boards don't populate `Location`, in which case you'll
get `null` and should fall back to `enp<bus>s<device>f<function>` derived
from `lspci` on the running OS instead.

**On top of that base name, systemd (>= 243, RHEL 8.3+) appends an extra
`np<port>` suffix** — e.g. `eno1np0`, `ens7f0np0` (note `f0` is kept
explicit here, unlike the plain `ens<slot>` case above) — whenever the
NIC driver exposes a devlink physical port name
(`/sys/class/net/<if>/phys_port_name`). This is a **kernel driver fact,
not anything in the DMTF Redfish schema** — no BMC can report it, and
`manufacturer` alone can't reliably predict it: confirmed on real
hardware from the SAME vendor going both ways — a dual-port Broadcom
add-in card genuinely has it (`eno1np0`/`eno2np1`,
`ens7f0np0`/`ens7f1np1`), while a plain embedded Broadcom 1GbE LOM on a
different box (`tg3`-driven, no devlink support at all) genuinely
doesn't (`eno1`/`eno2`, confirmed via that port's own systemd-generated
*altname*, which stays accurate even when something later renames the
primary interface — e.g. via agent-config's own MAC-based renaming, see
"Generating agent-config.yaml" below).

Because a wrong guess here breaks agent-config's interface matching
outright (worse than a cosmetically-off name), **this module does NOT
guess by default.** Set `devlink_port_naming: true` per host in
`inventory/hosts.yaml` (alongside `bmc_ip_hostname`, `node_labels`, etc.)
only after you've confirmed via a live boot that this host's NIC driver
actually exposes `phys_port_name` — then `likely_has_devlink_port_name()`
still narrows it to adapters whose `manufacturer` looks like
Broadcom/Mellanox/NVIDIA, so enabling it for a host doesn't force the
suffix onto every adapter that host has. Each port also carries a
`devlink_port_name_guessed: true/false` field reflecting whether the
suffix was actually applied, so it's easy to confirm what happened.

## MAC address cross-check (Dell iDRAC8)

No single Redfish network resource is reliable across every vendor and
firmware generation — confirmed on real Dell iDRAC8 hardware (PowerEdge
R630): the newer, richer `NetworkAdapters/.../Ports` schema (added to
the DMTF spec later, for PCI-slot/multi-port modeling) returned the
literal placeholder `00:00:00:00:00:00` for `AssociatedMACAddresses` on
every port of one adapter, and a raw `@odata.id` reference object
instead of an integer for `PhysicalPortNumber` — both symptoms of the
same underlying gap in that firmware's implementation of the newer
schema, even though the adapter itself was real and working. This
matches the reasoning behind Ironic/Sushy's own primary MAC-discovery
path (`Systems/{id}/EthernetInterfaces`), which is the older, simpler,
far more consistently-implemented DMTF resource — present since
Redfish 1.0. It has its own vendor-specific gaps too (an open Sushy bug
tracks Dell hardware splitting MAC and link/health data across two
separate `EthernetInterfaces` entries), which is exactly why this
module doesn't switch to it exclusively — it cross-references both:
`Systems/{id}/EthernetInterfaces` is fetched as a fallback source, and
for any port where `NetworkAdapters/Ports` data looks bogus
(placeholder or missing MAC), a matching `EthernetInterfaces` entry is
preferred instead, matched by Dell's own `<adapter-fqdd>-<port>-<function>`
Id convention. Each port carries a `mac_source` field (`null` normally,
otherwise a short explanation) so it's easy to see when this fallback
actually fired. `PhysicalPortNumber` is also never trusted by type
alone anymore — a non-integer value falls through the same
`port_number_from_id()`/positional fallback chain already used when
it's simply absent.

## ⚠️ Known limitation: disk `by-path` accuracy

The SCSI target portion of `/dev/disk/by-path/pci-...-scsi-H:C:T:L` (the
`T` — drive bay / logical-drive index) is reliably derivable from Redfish
via `PhysicalLocation.PartLocation.LocationOrdinalValue`.

The **PCI bus:device.function** portion, however, is *not* part of the
standard DMTF Redfish schema — it's an artifact of how the host BIOS
enumerates PCI at boot, and the BMC firmware isn't required to expose it.
This module opportunistically reads it from vendor OEM extensions
(`Oem.Dell.DellController` / `Oem.Hpe.DeviceInstance`), which iDRAC9+ and
recent iLO firmware generally provide. When that OEM data isn't present,
each drive/volume is still returned with the correct target/slot info, but
`predicted_by_path` may be `null` or approximate, and
`by_path_confidence` is set to `best_effort_pci_bdf_unknown` so you can
tell the difference programmatically.

**This is why `select_boot_disk()` doesn't rely on `by-path` unless it has
to** — see the next section. `wwn`/`serial_number`, when the BMC reports
them, sidestep this whole prediction problem entirely, since they're the
literal identifier the installed OS reports too, not a guess about PCI
enumeration.

For a guaranteed-correct `by-path` value specifically (useful even when
`wwn`/`serialNumber` are being used for `rootDeviceHints`, e.g. for your
own documentation or cross-checks), cross-check against `lspci` and
`ls -l /dev/disk/by-path/` once the OS is actually booted (a CoreOS live
ISO / rescue boot works well for this) — this playbook gets you there
before the OS exists (e.g. for kickstart/ignition/cloud-init disk
selection), but it's a prediction, not a live read.

Also note: for drives that are RAID members (`part_of_virtual_disk: true`),
the OS typically only sees the virtual disk's SCSI target, not each member
physical drive's — the individual physical drives in that case are visible
to the OS only via the controller's own tooling (`storcli`, `ssacli`, etc.),
not as separate `/dev/sdX` or `by-path` entries.

## Generating agent-config.yaml and install-config.yaml

`generate-agent-config.yaml` is a second, independent playbook that reads
`<cluster_dir>/output/hw_inventory.yaml` (written by `playbook.yaml` above)
and renders **two** OpenShift installer files in one run: an Agent-based
Installer `agent-config.yaml`, and an `install-config.yaml` (the
cluster-shape manifest — network CIDRs, VIPs, pull secret, SSH key — that
accompanies it). Run the discovery playbook first, edit the vars files
below, then:

```
./run-cluster.sh <cluster-name> agent-config
```

Output: `<cluster_dir>/output/agent-config.yaml` and
`<cluster_dir>/output/install-config.yaml`. Full worked examples (3
masters + 1 worker) are in `clusters/ocp-lab/output/agent-config.example.yaml`
and `clusters/ocp-lab/output/install-config.example.yaml`.

### Inputs you edit

| File | Purpose |
|---|---|
| `vars/cluster.yaml` | `cluster_name`, `rendezvous_ip`, `additional_ntp_sources`, cluster-wide `dns`, `gateway`, `network_profile` (topology + `link_aggregation`), and everything install-config.yaml needs (`base_domain`, `machine_network_cidr`, `api_vips`, `ingress_vips`, `pull_secret`, `ssh_key`, plus optional overrides — see below) |
| `inventory/hosts.yaml` | BMC endpoint + credentials, `masters`/`workers` groups (each host's `role:`, and `install-config.yaml`'s `compute[0].replicas`/`controlPlane.replicas` as a plain count of each group), **and** genuinely per-host network data only: each host's `network_members` (its own `{adapter_id, port}` hardware mapping) and its static `ip` |

Everything about *how* the network is built — bond vs standalone
ethernet, the bond's name, `mode`/`mtu`/LACP options, `vlan_id`, and the
cluster's subnet `prefix_length` — is defined **once**, in
`vars/cluster.yaml`'s `network_profile:` block, since OpenShift requires
every node on the same single network and doesn't support mixing bonded
and non-bonded nodes. Each host's `network_members` only holds what
actually differs per server: which physical NIC ports build that
network on this particular machine, and this machine's own IP.

`network_members` needs exactly 2+ entries when `network_profile.type`
is `bond`, or exactly 1 when it's `ethernet` — the filter checks this
and warns (rather than guessing) if the count is wrong. IPv4 is always
static — a single `ip` string per host — since `rendezvousIP` needs a
known address up front, so there's no DHCP option here. Any discovered
port not listed in `members` is automatically emitted as
`type: ethernet, state: down`.

### install-config.yaml fields

| `vars/cluster.yaml` key | install-config.yaml field | Required? |
|---|---|---|
| `base_domain` | `baseDomain` | Required |
| `machine_network_cidr` | `platform.baremetal.machineNetwork[0].cidr` | Required |
| `api_vips` (1 or 2 IPs) | `platform.baremetal.apiVIPs` | Required |
| `ingress_vips` (1 or 2 IPs) | `platform.baremetal.ingressVIPs` | Required |
| `pull_secret` | `pullSecret` | Required — vault this, it's tied to your Red Hat account |
| `ssh_key` | `sshKey` | Required |
| `cluster_network_cidr` | `networking.clusterNetwork[0].cidr` | Optional, defaults to `10.128.0.0/14` |
| `cluster_network_host_prefix` | `networking.clusterNetwork[0].hostPrefix` | Optional, defaults to `23` |
| `service_network_cidr` | `networking.serviceNetwork[0]` | Optional, defaults to `172.30.0.0/16` |
| `load_balancer_type` | `platform.baremetal.loadBalancer.type` | Optional, defaults to `OpenShiftManagedDefault` |

`networking.networkType` is always `OVNKubernetes` (OpenShift's current
default and only fully-supported CNI as of recent releases — not
something this repo needs to make configurable).

Before writing `install-config.yaml`, the playbook actually checks the
network makes sense, rather than trusting the numbers you typed:

- Every host's `network_members`/`ip` (in `inventory/hosts.yaml`) must fall
  inside `machine_network_cidr`.
- Every `api_vips`/`ingress_vips` entry must also fall inside
  `machine_network_cidr`.
- `api_vips`/`ingress_vips` must each have exactly 1 or 2 entries (IPv4,
  or IPv4+IPv6 for dual-stack).

A mismatch here — a VIP on the wrong subnet, a node IP that's actually
outside the machine network — wouldn't be caught by `install-config.yaml`
itself; it'd only surface much later, during actual cluster bring-up.
Catching it here means a config error fails the playbook run with a
message naming the exact bad value, not a stalled install hours in.

### Design decisions (the template left these open, so here's what I picked)

**Only one active network per cluster, enforced by the schema, not just a
runtime check.** OpenShift's Agent-based Installer only supports a
single network for the whole cluster at install time — an ethernet, an
ethernet+vlan, a bond, or a bond+vlan, never more than one, and never a
mix across nodes. `network_profile.type`/`name`/`vlan_id` in
`vars/cluster.yaml` are singular values, defined once — there's no way to
accidentally give one host a bond and another a standalone ethernet, or
two different bond names, since the schema has no per-host override for
any of it.

**`link_aggregation` is passed through as-is, not reconstructed field by
field.** `network_profile.link_aggregation` in `vars/cluster.yaml` is one
free-form block: the filter pulls `mode` out for nmstate's
`link-aggregation.mode`, and forwards every other key verbatim into
`link-aggregation.options` — nothing is hardcoded to 802.3ad's option set
(`lacp_rate`, `xmit_hash_policy`, ...). Switching to `mode: active-backup`
with `primary: eno1` instead just works, without touching the filter —
whatever real nmstate options your chosen mode needs, put them there and
they come through unchanged.

That passthrough is checked, not left silent: each option is validated
against a per-mode table (`BOND_OPTIONS_BY_MODE` /
`BOND_OPTIONS_COMMON` in `filter_plugins/agent_config_filters.py`) —
which options make sense for which mode genuinely differs (`lacp_rate`
only for `802.3ad`, `primary` only for `active-backup`/`balance-tlb`/
`balance-alb`, etc.) — catching wrong types, invalid enum values (e.g.
`lacp_rate: medium`), and likely typos in the option name early, in
`agent_config_warnings`. This is deliberately **non-blocking**: an
option this table doesn't recognize is passed through with a "verify
this" warning rather than rejected outright, since kernel bonding has
around 30 possible options and this table only covers the commonly used
ones — nmstate itself remains the final authority at apply time.

**DNS, the gateway, and the whole network topology are cluster-wide, not
per-host.** `dns:`, `gateway:`, and `network_profile:` all live once in
`vars/cluster.yaml`. Each host's `network_members`/`ip` in
`inventory/hosts.yaml` only holds what's actually different per server —
its own hardware mapping and its own `ip` — because that's genuinely
per-host data pulled from each server's own Redfish discovery, not a
deployment choice.

**`mac-address` vs `permanent-mac-address` follows real bonding
behavior, not "each interface's own MAC."** When a bond forms, Linux
makes it present the *first* member's MAC to the network as the bond's
active address - so `mac-address` on every bond member, the bond itself,
and any vlan stacked on top is that one shared value (the first
member's). Each member's own real hardware MAC is preserved separately
as `permanent-mac-address` - which is why the first member's two fields
match, but a second member's don't. `permanent-mac-address` only appears
on ethernet-type entries that are bond members; it's omitted on the bond
entry, the vlan entry, and on a standalone (non-bonded) ethernet
interface, since none of those have a second "permanent" MAC to
distinguish from.

**IPv4 is always static, no DHCP.** `rendezvousIP` requires a real,
known address before install even starts, so a per-host `ip:` string
plus the cluster-wide `network_profile.prefix_length` is all that's
needed — there's no DHCP branch to account for.

**`next-hop-interface` is derived automatically, not hand-typed.** A
host's `network_profile.type` + its own `members` resolve to a single
interface name — `bond0`, `ens2`, or a vlan sub-interface like
`bond0.100` — and that's what the cluster-wide default route (via
`gateway`) gets attached to. If a host is missing its `ip`, the route is
**omitted** for that host (not guessed) and a warning is printed — see
`agent_config_warnings` in the playbook output.

**Boot disk selection (`rootDeviceHints`)** — filters to disks that are
`>= 120 GB` (decimal, i.e. `120 * 10^9` bytes, matching how drive
capacity is normally advertised) and `bootable: true` — where `bootable`
itself is a heuristic (matching the disk's Id/SerialNumber as a literal
substring inside the BMC's `BootOptions` text), so when there's exactly
**one** size-eligible candidate and it didn't pass that check, it's used
anyway rather than reporting no boot disk found — there's no ambiguity
about *which* disk when it's the only one (confirmed on a real iDRAC9
with a single AHCI SATA disk whose `BootOptions` never textually matched
its Id/serial at all). Takes the first match, and picks **one**
`rootDeviceHints` field (the CRD supports several — `deviceName`, `wwn`,
`serialNumber`, `hctl`, `model`, `rotational`, `minSizeGigabytes` — but
this playbook only ever emits one, the most reliable one available) in
this priority order:

1. `wwn` — a hardware-intrinsic durable identifier (NAA/EUI/etc.), when
   the BMC's Redfish `Identifiers` array has one. This is *exactly* what
   the installed OS itself reports for that device (`lsblk`/`udevadm`
   `ID_WWN`), not a prediction — it can't be wrong the way `by-path` can
   be. Coverage varies a lot by controller vendor, especially for RAID
   virtual disks; when present, it's always preferred. A single-member
   RAWDEVICE/pass-through volume (a plain AHCI disk with no real RAID —
   confirmed on real hardware where the Volume's own `Identifiers` was
   empty) inherits its one physical drive's `wwn`/`serialNumber` at
   discovery time, since it's the exact same disk represented twice.
   **Format note:** Redfish's `Identifiers[].DurableName` for NAA-format
   WWNs is bare hex with no prefix (`50014ee0adfe2945`), but
   Ironic/Metal3's `rootDeviceHints.wwn` match is an *exact string
   compare* against what the OS itself reports — which, confirmed via a
   real disk's `udevadm`/`lsblk` output, is `0x`-prefixed
   (`0x50014ee0adfe2945`). `normalize_wwn()` adds that prefix at
   discovery time so the emitted hint actually matches; without it, the
   install would have silently failed to select the disk despite `wwn`
   looking like the most reliable possible hint.
2. `serialNumber` — same reasoning, for physical drives specifically
   (`Drive.SerialNumber` is far more consistently populated across
   vendors than a Volume's `Identifiers`). **Known caveat, unresolved:**
   on a real drive, this module's `serialNumber` (`WD WMAYP0000000`,
   straight from Redfish) did NOT exactly match what `lsblk`/`udevadm`
   showed for the same disk (`WD-WMAYP0000000` — hyphen, not space) —
   likely vendor/udev-specific serial-string formatting that isn't
   normalizable the way the `wwn` prefix issue above was (no single
   known transformation rule to apply generally). Since `wwn` is
   preferred whenever available, this only bites when a disk has no
   `wwn` at all (uncommon, but real) — verify a `serialNumber`-based hint
   against a live boot's `lsblk -o SERIAL` before relying on it.
3. `predicted_by_path`, when the discovery playbook found one — see
   "Known limitation: disk `by-path` accuracy" above for why this is a
   prediction, not a certainty, unlike 1 and 2.
4. `/dev/sda`, but **only** when it's the sole candidate disk (so there's
   no ambiguity about *which* disk `/dev/sda` would be).
5. Otherwise `null`, with a warning printed at the end of the playbook
   run asking for manual review. With multiple same-size candidate disks
   and no wwn/serial/by-path data, guessing kernel disk-letter order
   would risk silently pointing the installer at the wrong disk — that's
   worse than making you fill it in by hand.

**VLANs** — a `vlan_id` on a bond or standalone interface stacks a proper
nmstate `vlan` sub-interface on top (`bond0.100`) and moves the IPv4
config there; the base bond/interface itself carries no L3, matching how
nmstate expects a VLAN topology to look. This is also why the route's
`next-hop-interface` has to be derived (above) rather than fixed to a
bond's name — it needs to be `bond0.100`, not `bond0`, whenever a VLAN is
in play.

**Role** — comes from Ansible inventory group membership (`masters` /
`workers`), not a value baked into `hw_inventory.yaml`, since role is a
deployment decision, not a hardware fact. The playbook asserts every host
present in `hw_inventory.yaml` is in one of those two groups before it
writes anything.

**rendezvousIP sanity check** — the playbook collects every master's
actual configured static IP (from `inventory/hosts.yaml`) and asserts
`rendezvous_ip` matches one of them, failing fast with a clear message
if not, rather than writing an `agent-config.yaml` that points at an IP
no master will actually have.

Any boot-disk or bond-member-resolution problem is collected into
`agent_config_warnings` and printed at the end of the run — check that
output before applying the generated file.

## Verifying predictions against real hardware (live boot)

Everything above — `predicted_linux_ifname`, `predicted_by_path`,
`wwn`, `serial_number`, `devlink_port_name_guessed` — is a *prediction*
from Redfish data. Redfish also has no visibility at all into multipath,
Fibre Channel, or how the kernel's own drivers actually behave — that's
OS/HBA-driver territory, invisible to any BMC. The only way to get
ground truth for any of this is to actually boot the box and look.

This is a two-part system, built and tested end-to-end (including
transpiling the real Butane config with the actual `butane` binary, and
POSTing a realistic payload through the real webhook logic) short of
needing live BMC/web-server access to test the last mile:

- **`verify-boot/`** — a generic (no hostname, no per-host anything)
  Butane/Ignition config plus `collect-facts.sh`, a dependency-minimal
  bash script that gathers `lsblk -J`, `ip -j addr`, `ethtool -i` per
  NIC (driver name — the ground truth for `devlink_port_naming`),
  `multipath -ll`, `/sys/class/fc_host/*` (Fibre Channel), `nvme list -o
  json`, and `udevadm info` per block device, POSTs it all as one JSON
  blob to the webhook, then powers the machine off. Read-only — this
  boots the *live* ISO (RAM-only), never installs anything, so it's
  safe to run against a box that already has an OS on it.
- **`webhook/verify_webhook.py`** — a single stdlib-only Python process
  (plus PyYAML, already a hard dependency via Ansible itself). Its
  **only** job is receiving `collect-facts.sh`'s reports — it does not
  serve `verify.iso` or anything else. Host identification is
  **session-based**, not MAC-search-based: `mount-verify-media.yaml`
  generates a fresh random UUID for every run and registers it (`POST
  /session/register`, telling the webhook exactly which
  `hw_inventory.yaml` and `target_host` to expect) *before* anything
  boots; that UUID gets baked into that specific run's
  `collect-facts.sh` and sent back in its report, so matching is a
  direct dict lookup, not a search. This replaced an earlier
  MAC-overlap search across every cluster's `hw_inventory.yaml`, which
  had a real, confirmed failure mode on actual hardware: if the same
  physical MACs happened to also appear in a stale/duplicated
  `hw_inventory.yaml` under a *different* cluster (e.g. left over from
  an earlier cluster rename), the search could — and did — silently
  pick the wrong one, writing the report under the wrong
  cluster/hostname with no error at all. A payload with no session
  UUID at all (e.g. the shared, non-per-host ISO booted manually,
  outside `mount-verify-media.yaml`'s flow) falls back to that same
  MAC-search behavior — but the fallback now explicitly **rejects an
  ambiguous match** (the same MACs tied across two or more different
  clusters/hosts) instead of silently choosing one, listing every tied
  candidate in the error.

  Either way, it cross-checks every prediction against the live-boot
  ground truth:
  - writes a full report to `<cluster_dir>/output/verified/<hostname>.yaml`
  - **auto-corrects** confirmed interface-name mismatches directly in
    `hw_inventory.yaml` (backing up the original first, timestamped) —
    low risk, since a wrong name is just cosmetic/matchable-by-MAC
  - **does NOT auto-correct** disk-identifier mismatches (`wwn`/
    `serial_number`/`predicted_by_path`) — flagged in the report only,
    for manual review, since a wrong disk identifier changes *which
    physical disk* gets selected as the boot device
  - flags whether `devlink_port_naming` should actually be enabled or
    disabled for that host, based on the *real* `phys_port_name`/
    driver — not a guess
  - for the session-based path: also flags when the booted host's
    observed MACs have **zero overlap** with what's recorded for the
    `target_host` the session was registered for
    (`hardware_changed_warning` in the response/report) — since the
    session already tells us exactly which host to expect, that
    combination can only mean the hardware itself changed (NIC/board
    swap) since discovery last ran, not a mismatched identity
- **`manage-verify-iso.yaml`** — builds `verify.iso` **once per RHCOS
  version+arch**, not per host, and writes it into a plain, standard
  directory (`/var/www/html/coreos-isos` by default) for whatever web
  server you already run to serve — this playbook doesn't install,
  configure, or own a web server of any kind, only writes
  world-readable files into a directory you point it at. At real fleet
  scale (6–100+ servers pulling this ~1.3GB ISO concurrently during an
  install run), a mature web server's static-file path (Apache's
  worker/event MPM, native `Range`/`206` support) is the right tool for
  that load, which a hand-rolled single-process listener isn't built
  for — this is exactly the same reason `verify_webhook.py` stays
  narrowly scoped to the small, low-volume POST endpoint instead.
  Redfish virtual media boots over plain HTTP from any web server, so
  **no PXE/TFTP infrastructure is needed at all** either.

### Setup (once)

1. Deploy the webhook — the only infrastructure this subsystem actually
   adds; ISO serving reuses whatever web server you already have.
   `deploy-verify-webhook.yaml` runs it **as you** — the user who
   cloned this repo — reading `clusters/` directly from your own
   checkout (no dedicated system account, no copy under `/opt`, nothing
   to keep in sync). This is deliberate: multiple people can each clone
   this on their own machine and run their own webhook with zero shared
   setup, since it just uses whatever normal file permissions your
   checkout already has. It installs PyYAML via the OS package manager
   (not `pip` — this needs to work without PyPI access on airgapped/
   restricted boxes, same reasoning as everywhere else in this
   toolkit), renders its systemd unit, starts and enables the service,
   opens its port in the firewall (detects firewalld first — if this
   box uses something else, or a cloud security group instead, it
   tells you plainly rather than silently doing nothing or failing),
   and labels the port for SELinux if this box is in Enforcing mode (a
   Linux capability or an open firewall port alone is **not**
   sufficient on an enforcing box — SELinux is a separate layer
   requiring its own port-type label; detects Enforcing/Permissive/
   disabled first and skips cleanly rather than failing when it isn't
   relevant). Root is needed only for those last two steps and writing
   the systemd unit file — the service itself then runs entirely as
   you, no elevated privilege at runtime. **Run it with
   `--ask-become-pass`/`-K`, not literal `sudo ansible-playbook ...`**
   — the playbook captures your identity before any become elevation
   specifically so the service ends up running as you; invoking via
   plain `sudo` directly would see "root" instead:
   ```
   ansible-playbook deploy-verify-webhook.yaml --ask-become-pass
   ```
   Safe to re-run any time (every task is idempotent — tested
   explicitly, including the firewall and SELinux steps only
   adding/reloading when genuinely not already in place). Override the
   port with `-e verify_webhook_port=9090` if 8090 collides with
   something else on that box, or the OS package name with
   `-e verify_webhook_os_family=Debian` if this isn't a RHEL-family box.
2. Build a `verify.iso` for the RHCOS version you care about (once —
   reused for every host). Also needs root, to write under
   `/var/www/html` (or wherever you point `iso_web_root`):
   ```
   ansible-playbook manage-verify-iso.yaml --ask-become-pass \
     -e rhcos_iso_url='https://mirror.openshift.com/pub/openshift-v4/dependencies/rhcos/4.20/latest/rhcos-live-iso.x86_64.iso' \
     -e rhcos_version=4.20 \
     -e webhook_url='http://<this-host>:8090'
   ```
   If `/var/www/html` is already Apache's docroot on that box, the ISO
   is immediately servable at
   `http://<this-host>/coreos-isos/4.20/x86_64/verify.iso` — nothing
   else to configure. Override `-e iso_web_root=...` if your web
   server's docroot is somewhere else instead.

### Per-host verification

**Networking note first**: the base Ignition has no network config at
all — it relies on DHCP. On a statically-addressed production network
(the normal case — see `cluster.yaml`'s `machine_network_cidr` and
every host's own static `ip` in `inventory/hosts.yaml`), that means the
live boot gets **no usable address at all** on its real NICs, only
whatever link-local address its BMC's own USB NIC hands out (confirmed
directly: `collect-facts.sh` retrying against the webhook forever, `ip
addr` on the console showing only a `169.254.x.x` address on the BMC's
USB interface — not a real network path anywhere). `mount-verify-media.yaml`
handles this automatically: it derives a small per-host static-IP
NetworkManager keyfile from the exact same data already in
`inventory/hosts.yaml` and `vars/cluster.yaml` — matched by **MAC
address**, not a name prediction, since that's exactly the kind of
thing this tool exists to verify, not assume.

**Session note second**: every run generates a fresh random UUID,
registers it with the webhook (telling it exactly which
`hw_inventory.yaml`/`target_host` to expect) *before* anything boots,
and bakes that UUID into a **fresh per-run Ignition transpile** — this
playbook re-runs `butane` itself every time (auto-downloading it if not
already present, same as `manage-verify-iso.yaml`), rather than reusing
a persisted config, specifically so the UUID ends up inside
`collect-facts.sh`. That UUID is what makes host matching on the
webhook side a direct lookup instead of a MAC-overlap search across
every cluster — which had a real, confirmed failure mode on actual
hardware (a stale/duplicated `hw_inventory.yaml` under a different
cluster, sharing the same physical MACs, silently won the match) — and
what this playbook itself polls afterward for the result, rather than
a host's static IP (which gets reused across every run against the
same box, so a stale result from an earlier attempt could otherwise
answer a brand new poll instantly).

Both pieces — the network keyfile and this run's Ignition — go into
ONE combined `coreos-installer iso customize --live-ignition ...
--network-keyfile ...` call, built fresh from `base-live.iso` every
time. Built fresh, not layered onto anything: `iso customize` is
`coreos-installer`'s single authoritative "configure everything in one
pass" command, and running it a second time on an ISO a prior
`customize`/`ignition embed` call already touched is not a
documented-safe composition — confirmed on real hardware to silently
clobber the previously-set Ignition (`Ignition: no config provided by
user` at boot, despite the embed step itself reporting success).
`base-live.iso` stays the single source of truth either way; nothing
about `manage-verify-iso.yaml`'s one-per-version design changes.

Prefer the `run-cluster.sh` wrapper — it derives `-i` and `cluster_dir`
from one cluster name the same way it already does for `discover`/
`agent-config`/`acm-manifests`, so they can't end up pointing at two
different clusters (an easy mistake to make passing them separately by
hand, and one that fails confusingly deep into a run rather than
immediately):
```
./run-cluster.sh ocp-lab mount-verify-media -- \
  -e target_host=server01-bmc \
  -e rhcos_version=4.20 \
  -e webhook_host=<host-your-BMCs-can-actually-reach>
```
Or call the playbook directly if you need to (e.g. a custom `-i`):
```
ansible-playbook mount-verify-media.yaml --ask-become-pass \
  -i clusters/ocp-lab/inventory/hosts.yaml \
  -e cluster_dir=clusters/ocp-lab \
  -e target_host=server01-bmc \
  -e rhcos_version=4.20 \
  -e webhook_host=<host-your-BMCs-can-actually-reach>
```
(`webhook_host` isn't necessarily the same address your Ansible control
node uses to reach the BMC — it's the address *the BMC's own management
network* can reach *this* box at, which can genuinely be a different
path; see the troubleshooting note below.)

The host boots, reports, and powers itself off automatically — this
task itself waits for and shows the actual result (polling the
webhook's own record of this run, not just a timeout), typically a few
minutes. `<cluster_dir>/output/verified/<hostname>.yaml` holds the same
result afterward if you need it again.

**Bonded networks**: this only brings up the one "boot" NIC (the same
primary member `generate-agent-config.yaml`/ACM manifests already treat
as primary) with the full host IP statically assigned — it does not
replicate a full bond. Usually fine for a diagnostic boot, but if your
switches strictly enforce LACP before passing any traffic on a bond
member port, this may not get you connectivity either — that's a
switch-config question, not something this playbook can work around.

### Not yet covered / worth knowing

- **Vendor coverage**: `mount-verify-media.yaml`'s Redfish virtual-media
  sequence (InsertMedia/boot-override/Reset) matches Dell iDRAC9 exactly
  (this repo's own confirmed usage elsewhere). HPE iLO/Lenovo XCC
  implement the same DMTF-standard actions but at different endpoint
  paths — adjust `bmc_manager_path`/`bmc_system_path`/
  `bmc_virtual_media_id` for those vendors; not verified against real
  HPE/Lenovo hardware here.
- **ISO version strategy**: a verification boot isn't installing
  anything, so it mostly just needs *recent-enough kernel drivers* for
  your hardware — it doesn't need to exactly match every OCP version
  you're targeting. Consider keeping a small rolling set (e.g. the
  newest supported minor per architecture) rather than one ISO per every
  OCP version ever used, to keep `manage-verify-iso.yaml`'s storage
  footprint down.
- **`multipath`/FC/NVMe**: `collect-facts.sh` gathers this (see above),
  and it's in every report, but `verify_webhook.py` doesn't cross-check
  it against anything yet — `hw_inventory.yaml`/`redfish_hw_facts.py`
  don't discover multipath/FC topology via Redfish at all currently
  (Redfish's `Storage`/`Volumes` schema doesn't really model HBA/FC
  fabric topology the way it models local drives). The raw data is
  there in the report for now; a future pass could add FC WWPN
  discovery via `SimpleStorage`/`FibreChannel` Redfish resources where
  BMCs expose them, and cross-check against what's actually observed.

## Secrets: pull secret, SSH key, and real cluster data

Only the two example clusters, `clusters/ocp-lab/` and
`clusters/ocp-lab-kvm/`, are published. Every other directory under
`clusters/` is a working or real cluster and stays local: `.gitignore`
ignores `clusters/*` except those two, so a new cluster directory is
private the moment it's created - nothing to add per cluster.

The generators leave `CHANGE_ME` placeholders for the pull secret and SSH
key. Fill them into a real cluster's generated files with
`inject-secrets.sh`, which reads both from files outside the repo:

```
mkdir -p ~/.openshift && chmod 700 ~/.openshift
cp pull-secret.txt ~/.openshift/pull-secret.txt && chmod 600 ~/.openshift/pull-secret.txt

./run-cluster.sh <cluster-name> all
./inject-secrets.sh <cluster-name>
```

| File | Fields `inject-secrets.sh` fills in |
|---|---|
| `output/install-config.yaml` | `pullSecret`, `sshKey` |
| `output/acm/cluster.yaml` | both `kubernetes.io/dockerconfigjson` Secrets' `.dockerconfigjson` (base64), `InfraEnv` `spec.sshAuthorizedKey`, `AgentClusterInstall` `spec.sshPublicKey` |

- Defaults are `~/.openshift/pull-secret.txt` and `~/.ssh/id_rsa.pub`;
  override with `PULL_SECRET_FILE=...` / `SSH_KEY_FILE=...`.
- It refuses to write into any file that is tracked or not git-ignored
  (so it can't touch the published examples), rejects a private key or a
  file that isn't a pull secret, and leaves the patched files mode 600.
- Needs mikefarah `yq` v4. The `yq` from `dnf`/`pip` is a different tool
  (a Python `jq` wrapper) and the script stops with install instructions
  if that's the one on `PATH`; `YQ=/path/to/yq` picks a specific binary.

`githooks/pre-commit` is a second line of defence: it blocks a commit
that stages anything under `clusters/` outside the two examples (even
with `git add -f`), or adds a real pull secret (JSON or base64), a full
SSH public key, or a private key. Enable it once per clone:

```
git config core.hooksPath githooks
```

`git commit --no-verify` skips it for a commit you're sure about.

## Files

```
redfish-hw-inventory/
├── ansible.cfg                      # no default inventory - see run-cluster.sh
├── run-cluster.sh                     # wrapper: derives -i and cluster_dir from one cluster name
├── inject-secrets.sh                  # fills the real pull secret + SSH key into generated output - see "Secrets"
├── githooks/pre-commit                # blocks commits of real cluster data / secrets - see "Secrets"
├── playbook.yaml                      # stage 1: hardware discovery (cluster-agnostic)
├── generate-agent-config.yaml         # stage 2: agent-config.yaml + install-config.yaml (cluster-agnostic)
├── generate-acm-manifests.yaml        # stage 3: ACM/Assisted-Installer CRs (cluster-agnostic)
├── convert-clusters.yaml               # one-off: clusters/convert/*.yaml (legacy format) -> clusters/<name>/
├── tasks/convert_one_cluster.yaml      # convert-clusters.yaml's per-file conversion logic
├── manage-verify-iso.yaml             # builds verify.iso once per RHCOS version+arch - see "Verifying predictions..."
├── mount-verify-media.yaml            # derives per-host static-IP ISO + mounts via Redfish virtual media + boots it
├── deploy-verify-webhook.yaml         # installs the webhook as a systemd service + opens its firewall port
├── library/redfish_hw_facts.py             # stage 1: all the Redfish discovery work
├── filter_plugins/agent_config_filters.py  # stage 2: boot-disk + nmstate + CIDR-check logic
├── filter_plugins/acm_manifest_filters.py  # stage 3: bootMACAddress + BMC Redfish URI logic
├── verify-boot/
│   ├── verify.bu                     # generic Butane config embedded into verify.iso
│   └── collect-facts.sh              # runs on the live boot, POSTs facts to the webhook, powers off
├── webhook/
│   ├── verify_webhook.py             # receives collect-facts.sh reports only - not an ISO server
│   └── verify-webhook.service.j2     # systemd unit template, rendered by deploy-verify-webhook.yaml
├── tools/readme_to_adf.py                  # converts this README to ADF for Confluence - see footnote [^1]
├── README.adf.json                         # this README, pre-converted to ADF
└── clusters/
    ├── convert/                     # legacy-format cluster files awaiting convert-clusters.yaml (local only, git-ignored)
    ├── ocp-lab/                     # example: real hardware, bonded network
    │   ├── inventory/hosts.yaml      # this cluster's BMCs, credentials (bmc_hosts.vars, overridable per host), masters/workers groups
    │   ├── vars/
    │   │   ├── cluster.yaml          # cluster/network/install-config/ACM settings - see below
    │   └── output/
    │       ├── hw_inventory.example.yaml
    │       ├── agent-config.example.yaml
    │       ├── install-config.example.yaml
    │       └── acm/                  # generate-acm-manifests.yaml output (cluster.yaml + hosts/<fqdn>.yaml)
    └── ocp-lab-kvm/                 # example: sushy-tools/KVM lab, standalone ethernet
        ├── inventory/hosts.yaml       # network_members use the manual 'name:' override (see sushy-tools section)
        ├── vars/cluster.yaml
        └── output/
            ├── hw_inventory.example.yaml
            ├── agent-config.example.yaml
            ├── install-config.example.yaml
            └── acm/
```

Add a new cluster by copying one of these two directories to
`clusters/<your-cluster-name>/` and editing its contents — see "Managing
multiple clusters" above.

---

[^1]: **Importing this README into Confluence.** `README.adf.json` is
    this file converted to [Atlassian Document Format](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/)
    (ADF) — Confluence Cloud's native rich-text JSON format — by
    `tools/readme_to_adf.py`. Re-run that script after editing README.md
    to regenerate it:
    ```
    python3 tools/readme_to_adf.py README.md > README.adf.json
    ```
    To create a Confluence page from it, `POST` to the
    [Confluence REST API v2 pages endpoint](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/#api-pages-post),
    with the ADF document JSON-encoded as a **string** inside
    `body.value` (not nested as a raw object — the API rejects that):
    ```
    curl -X POST \
      'https://<your-site>.atlassian.net/wiki/api/v2/pages' \
      -u '<your-email>:<your-api-token>' \
      -H 'Content-Type: application/json' \
      -d @- <<PAYLOAD
    {
      "spaceId": "<numeric-space-id>",
      "status": "current",
      "title": "redfish-hw-inventory README",
      "body": {
        "representation": "atlas_doc_format",
        "value": $(python3 -c "import json,sys; print(json.dumps(open('README.adf.json').read()))")
      }
    }
    PAYLOAD
    ```
    Notes: `<your-api-token>` is created at
    [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens);
    `spaceId` is the numeric ID of the target space (find it via
    `GET /wiki/api/v2/spaces?keys=<SPACEKEY>`); the `$(python3 ...)`
    subshell re-serializes the ADF file's contents as a properly
    JSON-escaped string, which is what makes `body.value` a string
    containing JSON rather than nested JSON — the exact requirement the
    v2 API enforces. To update an existing page instead of creating one,
    use `PUT /wiki/api/v2/pages/{id}` with the same `body` shape plus a
    `version.number` incremented from the page's current version.
