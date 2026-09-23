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
SESSION_UUID="__SESSION_UUID__"
LOG_TAG="verify-boot"
MAX_ATTEMPTS=5
RETRY_DELAY=10

log() { logger -t "$LOG_TAG" -- "$*"; echo "[$LOG_TAG] $*"; }

b64() { base64 -w0 2>/dev/null || base64; }  # -w0 not on every base64; fall back

# Runs a command that's supposed to print JSON on success. Correctly
# handles the case (confirmed on real hardware, via nvme-cli specifically:
# it prints {"error": "..."} to STDOUT and STILL exits non-zero when it
# can't enumerate anything) where a command prints something to stdout
# AND fails - the naive 'cmd 2>/dev/null || echo null' pattern this
# replaced only checks the exit code, so on that combination it doesn't
# discard the already-printed output, it APPENDS 'null' right after it -
# two JSON values silently concatenated into one field, invalid JSON the
# instant it's embedded as a single value. This discards the command's
# entire stdout whenever the exit code is non-zero (or nothing was
# printed at all), full stop - 'null' only, never a mix of the two.
json_or_null() {
  local out rc
  out=$("$@" 2>/dev/null)
  rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
    printf '%s' "$out"
  else
    printf 'null'
  fi
}

# ---- disks --------------------------------------------------------------
LSBLK_JSON=$(json_or_null lsblk -J -O)

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
  NVME_JSON=$(json_or_null nvme list -o json)
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
IP_ADDR_JSON=$(json_or_null ip -j addr)
IP_LINK_JSON=$(json_or_null ip -j link)

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
  "session_uuid": "${SESSION_UUID}",
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

# Send the whole payload base64-wrapped, not raw JSON - this makes the
# webhook's own decode step (not this script) the single place that
# turns bytes back into JSON, and lets it log the EXACT raw content on a
# parse failure (both the base64 as received and the decoded text) for
# direct inspection via journalctl, instead of guessing blind at what
# might be wrong. The base64 alphabet itself can't be corrupted by
# anything that would break JSON, so this also rules transport-layer
# mangling in or out cleanly, separate from whether the CONTENT is
# genuinely malformed JSON.
PAYLOAD_B64=$(printf '%s' "$PAYLOAD" | b64)

sent=false
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  log "POSTing facts to ${WEBHOOK_URL} (attempt ${attempt}/${MAX_ATTEMPTS})"
  http_code=$(curl -s -o /tmp/webhook_response.json -w '%{http_code}' --max-time 30 \
    -X POST -H 'Content-Type: text/plain' \
    --data-binary "$PAYLOAD_B64" "${WEBHOOK_URL%/}/verify" 2>&1)
  curl_rc=$?
  # The collector's job is to collect and deliver - period. Whether the
  # webhook then says "matched host X" or "no host matched these MACs"
  # or anything else is the LISTENER's business logic, not this script's
  # concern, and retrying an IDENTICAL payload 5 times against that same
  # business-logic outcome accomplishes nothing except wasting ~50s and
  # muddying the log with repeated "failures" that were never failures
  # of collection or delivery at all. So: success here means the webhook
  # was actually REACHED and RESPONDED - any HTTP status code at all,
  # not specifically 200. The one thing actually worth retrying is a
  # genuine delivery failure: curl never got a response back (connection
  # refused/timed out/no route) - that's the only case where trying
  # again might plausibly get a different outcome.
  if [ "$curl_rc" -eq 0 ]; then
    log "Webhook reached and responded: HTTP ${http_code}. Response body: $(cat /tmp/webhook_response.json 2>/dev/null | head -c 500)"
    sent=true
    break
  fi
  log "Webhook POST failed: curl exit code ${curl_rc} (no HTTP response received - connection/network problem, not a webhook-side error). curl said: ${http_code}"
  log "Retrying in ${RETRY_DELAY}s"
  sleep "$RETRY_DELAY"
done

if [ "$sent" != true ]; then
  log "FAILED after ${MAX_ATTEMPTS} attempts - never reached the webhook at all (delivery failure, not a data/matching problem). Powering off anyway."
fi

# Give journald a moment to flush console output, and a real window to
# actually read/screenshot the result on the console before it's gone -
# 15s rather than the previous 3s, which was only ever meant to flush
# logs, not give a human time to look at anything.
log "Powering off in 15s - read/screenshot this now if you need to."
sleep 15
systemctl poweroff
