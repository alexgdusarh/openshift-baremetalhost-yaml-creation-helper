#!/bin/bash
# collect-facts.sh
#
# Runs once, early, during a CoreOS *live* boot (not an install - nothing
# here writes to disk). Gathers ground-truth disk/network/multipath/FC/
# NVMe facts using only tools RHCOS/FCOS ship by default (bash, curl,
# lsblk, ip, udevadm, ethtool; multipath/nvme CLIs are used opportunistically
# and skipped if absent), POSTs them as one JSON blob to the verification
# webhook, then powers the machine off.
#
# Deliberately does NO parsing/interpretation here - every command's
# output goes over the wire close to raw (JSON where the tool already
# emits it - lsblk -J, ip -j; base64-wrapped raw text otherwise, so
# newlines/quoting can never produce broken JSON). All interpretation
# (matching against hw_inventory.yaml, flagging mismatches) happens
# server-side in the webhook, where it's easy to fix and redeploy without
# needing another boot cycle.
#
# This script is generic - it does NOT know which inventory_hostname it
# is. It reports every discovered MAC address; the webhook identifies the
# host by matching those MACs against hw_inventory.yaml (the same data
# Redfish discovery already produced), so ONE verify.iso works for every
# host in the fleet - no per-host Ignition/ISO needed.

set -u
WEBHOOK_URL="${WEBHOOK_URL:-__WEBHOOK_URL__}"
LOG_TAG="verify-boot"
MAX_ATTEMPTS=5
RETRY_DELAY=10

log() { logger -t "$LOG_TAG" -- "$*"; echo "[$LOG_TAG] $*"; }

b64() { base64 -w0 2>/dev/null || base64; }  # -w0 not on every base64; fall back

# ---- disks --------------------------------------------------------------
LSBLK_JSON=$(lsblk -J -O 2>/dev/null || echo 'null')

BY_ID=$(ls -la /dev/disk/by-id/ 2>/dev/null | b64)
BY_PATH=$(ls -la /dev/disk/by-path/ 2>/dev/null | b64)

# multipath: only meaningful if device-mapper-multipath is present AND has
# something configured - absent on most single-path SATA/SAS boxes, which
# is itself useful information (confirms "no multipath here"), not an error.
if command -v multipath >/dev/null 2>&1; then
  MULTIPATH_RAW=$(multipath -ll 2>&1)
else
  MULTIPATH_RAW="multipath: command not present on this image"
fi
MULTIPATH_B64=$(printf '%s' "$MULTIPATH_RAW" | b64)

# NVMe: nvme-cli ships on RHCOS for NVMe-oF support; -o json avoids any
# text-parsing on our side entirely.
if command -v nvme >/dev/null 2>&1; then
  NVME_JSON=$(nvme list -o json 2>/dev/null || echo 'null')
else
  NVME_JSON='null'
fi

# Fibre Channel: no CLI tool needed - FC HBA ports show up directly under
# sysfs regardless of vendor (qla2xxx, lpfc, etc.) once the driver binds.
FC_RAW=""
for h in /sys/class/fc_host/*; do
  [ -d "$h" ] || continue
  name=$(basename "$h")
  FC_RAW="${FC_RAW}== ${name} ==
port_name: $(cat "$h/port_name" 2>/dev/null)
node_name: $(cat "$h/node_name" 2>/dev/null)
port_state: $(cat "$h/port_state" 2>/dev/null)
port_type: $(cat "$h/port_type" 2>/dev/null)
speed: $(cat "$h/speed" 2>/dev/null)
fabric_name: $(cat "$h/fabric_name" 2>/dev/null)
"
done
[ -z "$FC_RAW" ] && FC_RAW="no /sys/class/fc_host entries - no FC HBA detected/bound"
FC_B64=$(printf '%s' "$FC_RAW" | b64)

# udevadm dump per block device - the definitive ID_SERIAL/ID_WWN/ID_MODEL
# source of truth, same property namespace Ironic/Metal3 rootDeviceHints
# actually match against.
UDEV_RAW=""
for d in /sys/block/sd* /sys/block/nvme* /sys/block/dm-*; do
  [ -e "$d" ] || continue
  n=$(basename "$d")
  # dm-* devices only matter if they're multipath maps; skip LVM/other
  # dm noise by checking DM_UUID prefix once we have the info anyway -
  # simplest to just include them all, webhook can filter.
  UDEV_RAW="${UDEV_RAW}== /dev/${n} ==
$(udevadm info --query=all --name="/dev/${n}" 2>/dev/null)
"
done
UDEV_B64=$(printf '%s' "$UDEV_RAW" | b64)

# ---- network --------------------------------------------------------------
IP_ADDR_JSON=$(ip -j addr 2>/dev/null || echo 'null')
IP_LINK_JSON=$(ip -j link 2>/dev/null || echo 'null')

# ethtool -i per interface: the ground truth for driver name, which is
# exactly what settles a devlink_port_naming (npX suffix) guess - a real
# driver: bnxt_en/mlx5_core here CONFIRMS it; tg3/i40e/etc. CONFIRMS it
# should stay off. Also grab phys_port_name directly (empty/absent means
# no devlink port name, regardless of driver).
ETHTOOL_RAW=""
for i in /sys/class/net/*; do
  n=$(basename "$i")
  [ "$n" = "lo" ] && continue
  ETHTOOL_RAW="${ETHTOOL_RAW}== ${n} ==
$(ethtool -i "$n" 2>&1)
phys_port_name: $(cat "$i/phys_port_name" 2>/dev/null || echo '(absent)')
pci_address: $(basename "$(readlink -f "$i/device" 2>/dev/null)" 2>/dev/null)
"
done
ETHTOOL_B64=$(printf '%s' "$ETHTOOL_RAW" | b64)

# ---- identity (best-effort only - webhook correlates by MAC, not this) --
HOSTNAME_VAL=$(hostname 2>/dev/null || echo unknown)
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)
PRODUCT_UUID=$(cat /sys/class/dmi/id/product_uuid 2>/dev/null || echo unknown)
PRODUCT_SERIAL=$(cat /sys/class/dmi/id/product_serial 2>/dev/null || echo unknown)

# ---- assemble and send ----------------------------------------------------
PAYLOAD=$(cat <<PAYLOAD_EOF
{
  "hostname_hint": "${HOSTNAME_VAL}",
  "boot_id": "${BOOT_ID}",
  "product_uuid": "${PRODUCT_UUID}",
  "product_serial": "${PRODUCT_SERIAL}",
  "collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "lsblk": ${LSBLK_JSON},
  "ip_addr": ${IP_ADDR_JSON},
  "ip_link": ${IP_LINK_JSON},
  "nvme_list": ${NVME_JSON},
  "by_id_b64": "${BY_ID}",
  "by_path_b64": "${BY_PATH}",
  "multipath_b64": "${MULTIPATH_B64}",
  "fc_hosts_b64": "${FC_B64}",
  "udevadm_b64": "${UDEV_B64}",
  "ethtool_b64": "${ETHTOOL_B64}"
}
PAYLOAD_EOF
)

sent=false
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  log "POSTing facts to ${WEBHOOK_URL} (attempt ${attempt}/${MAX_ATTEMPTS})"
  if curl -sf --max-time 30 -X POST -H 'Content-Type: application/json' \
       --data-binary "$PAYLOAD" "${WEBHOOK_URL%/}/verify"; then
    log "Webhook accepted the payload."
    sent=true
    break
  fi
  log "Webhook POST failed, retrying in ${RETRY_DELAY}s"
  sleep "$RETRY_DELAY"
done

if [ "$sent" != true ]; then
  log "FAILED after ${MAX_ATTEMPTS} attempts - facts were NOT recorded. Powering off anyway."
fi

# Give journald a moment to flush console output before the machine drops.
sleep 3
systemctl poweroff
