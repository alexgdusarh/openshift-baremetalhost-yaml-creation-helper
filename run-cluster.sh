#!/usr/bin/env bash
# run-cluster.sh - single entry point for running either playbook against
# one cluster, without having to pass -i and -e cluster_dir separately
# every time (they're always derived from the same clusters/<name>/ path).
#
# Usage:
#   ./run-cluster.sh <cluster-name> discover [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> agent-config [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> vsphere-ipi [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> acm-manifests [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> mount-verify-media -- -e target_host=<host> -e rhcos_version=<version> -e webhook_host=<host-your-BMCs-can-reach>
#   ./run-cluster.sh <cluster-name> all [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> clients [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> agent-iso [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> agent-boot -- -e iso_http_host=<addr> [-e confirm=true]
#   ./run-cluster.sh <cluster-name> convert [-- extra ansible-playbook args]
#
# vsphere-ipi renders output/vsphere-ipi/install-config.yaml for a vSphere
# IPI install with static node IPs from the same cluster data - see
# generate-vsphere-ipi-config.yaml. Not part of `all`.
#
# clients downloads oc/kubectl, openshift-install, oc-mirror and opm for the
# cluster's ocp_version - see download-clients.yaml (for a release without
# a cluster: ansible-playbook download-clients.yaml -e clients_version=X).
# agent-iso runs openshift-install agent create image on the files
# agent-config wrote - see create-agent-iso.yaml. agent-boot mounts that
# ISO on every BMC and boots from it - see boot-agent-iso.yaml; it is a
# dry run unless -e confirm=true. Neither is part of `all`.
#
# convert turns clusters/convert/<cluster-name>.yaml (legacy format) into
# clusters/<cluster-name>/ - see convert-clusters.yaml. It is the only
# action that runs before clusters/<cluster-name>/ exists. Pass a
# comma-separated list (a,b,c) to convert several in one run, or run
# ansible-playbook convert-clusters.yaml directly to convert every file.
#
# Examples:
#   ./run-cluster.sh ocp-lab discover
#   ./run-cluster.sh ocp-lab agent-config
#   ./run-cluster.sh ocp-lab acm-manifests
#   ./run-cluster.sh ocp-lab all
#   ./run-cluster.sh ocp-lab discover -- --ask-vault-pass
#   ./run-cluster.sh ocp-lab-kvm discover -- -e bmc_password=password
#   ./run-cluster.sh ocp5 mount-verify-media -- -e target_host=master-00 -e rhcos_version=4.20 -e webhook_host=192.168.0.1
#
# mount-verify-media specifically exists to prevent exactly the mistake
# that motivated adding it: -i and -e cluster_dir have to agree with
# each other (both come from clusters/<cluster-name>/), and passing them
# separately by hand is exactly how they end up pointing at two
# different clusters without any error until deep into a run. Deriving
# both from one name makes that class of mistake impossible - target_host,
# rhcos_version, and webhook_host are the only things actually specific
# to a single verification run, so those are what's left to pass by hand.
#
# Expects clusters/<cluster-name>/inventory/hosts.yaml to exist - see
# README.md's "Managing multiple clusters" section for the full layout.

set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 <cluster-name> <discover|agent-config|vsphere-ipi|acm-manifests|clients|agent-iso|agent-boot|mount-verify-media|all|convert> [-- extra ansible-playbook args]

Examples:
  $0 ocp-lab discover
  $0 ocp-lab agent-config
  $0 ocp-lab all
  $0 ocp-lab discover -- --ask-vault-pass
  $0 ocp5 mount-verify-media -- -e target_host=master-00 -e rhcos_version=4.20 -e webhook_host=192.168.0.1
  $0 ocp-lab vsphere-ipi
  $0 ocp-lab clients
  $0 ocp-lab agent-iso
  $0 ocp-lab agent-boot -- -e iso_http_host=10.0.0.5               # dry run
  $0 ocp-lab agent-boot -- -e iso_http_host=10.0.0.5 -e confirm=true
  $0 ocp-new convert          # clusters/convert/ocp-new.yaml -> clusters/ocp-new/
EOF
  exit 1
}

[ "$#" -ge 2 ] || usage

CLUSTER_NAME="$1"
ACTION="$2"
shift 2

# Everything after an optional literal "--" is passed straight through to
# ansible-playbook (e.g. --ask-vault-pass, -e bmc_password=...).
if [ "${1:-}" = "--" ]; then
  shift
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_DIR="clusters/${CLUSTER_NAME}"
INVENTORY="${CLUSTER_DIR}/inventory/hosts.yaml"

cd "$REPO_ROOT"

# convert creates clusters/<cluster-name>/, so it can't require it to exist.
if [ "$ACTION" = "convert" ]; then
  # JSON form so a list with spaces ("a, b") stays one value.
  exec ansible-playbook convert-clusters.yaml -e "{\"convert_only\": \"${CLUSTER_NAME}\"}" "$@"
fi

if [ ! -d "$CLUSTER_DIR" ]; then
  echo "error: ${CLUSTER_DIR} does not exist." >&2
  echo "Expected layout: clusters/<cluster-name>/{inventory,group_vars,vars,output}/..." >&2
  exit 1
fi

if [ ! -f "$INVENTORY" ]; then
  echo "error: ${INVENTORY} not found." >&2
  exit 1
fi

run_discover() {
  ansible-playbook playbook.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

run_agent_config() {
  ansible-playbook generate-agent-config.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

run_vsphere_ipi() {
  ansible-playbook generate-vsphere-ipi-config.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

run_acm_manifests() {
  ansible-playbook generate-acm-manifests.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

run_clients() {
  ansible-playbook download-clients.yaml -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

run_agent_iso() {
  ansible-playbook create-agent-iso.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

run_agent_boot() {
  ansible-playbook boot-agent-iso.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

run_mount_verify_media() {
  ansible-playbook mount-verify-media.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
}

case "$ACTION" in
  discover)
    run_discover "$@"
    ;;
  agent-config)
    run_agent_config "$@"
    ;;
  vsphere-ipi)
    run_vsphere_ipi "$@"
    ;;
  acm-manifests)
    run_acm_manifests "$@"
    ;;
  clients)
    run_clients "$@"
    ;;
  agent-iso)
    run_agent_iso "$@"
    ;;
  agent-boot)
    run_agent_boot "$@"
    ;;
  mount-verify-media)
    if [ "$#" -eq 0 ]; then
      echo "error: mount-verify-media needs -e target_host=<host> -e rhcos_version=<version> -e webhook_host=<host> after --" >&2
      usage
    fi
    run_mount_verify_media "$@"
    ;;
  all)
    run_discover "$@"
    run_agent_config "$@"
    run_acm_manifests "$@"
    ;;
  *)
    echo "error: unknown action '${ACTION}' (expected discover, agent-config, vsphere-ipi, acm-manifests, clients, agent-iso, agent-boot, mount-verify-media, all, or convert)" >&2
    usage
    ;;
esac
