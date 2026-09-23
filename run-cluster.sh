#!/usr/bin/env bash
# run-cluster.sh - single entry point for running either playbook against
# one cluster, without having to pass -i and -e cluster_dir separately
# every time (they're always derived from the same clusters/<name>/ path).
#
# Usage:
#   ./run-cluster.sh <cluster-name> discover [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> agent-config [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> acm-manifests [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> mount-verify-media -- -e target_host=<host> -e rhcos_version=<version> -e webhook_host=<host-your-BMCs-can-reach>
#   ./run-cluster.sh <cluster-name> all [-- extra ansible-playbook args]
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
Usage: $0 <cluster-name> <discover|agent-config|acm-manifests|mount-verify-media|all> [-- extra ansible-playbook args]

Examples:
  $0 ocp-lab discover
  $0 ocp-lab agent-config
  $0 ocp-lab all
  $0 ocp-lab discover -- --ask-vault-pass
  $0 ocp5 mount-verify-media -- -e target_host=master-00 -e rhcos_version=4.20 -e webhook_host=192.168.0.1
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

run_acm_manifests() {
  ansible-playbook generate-acm-manifests.yaml -i "$INVENTORY" -e "cluster_dir=${CLUSTER_DIR}" "$@"
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
  acm-manifests)
    run_acm_manifests "$@"
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
    echo "error: unknown action '${ACTION}' (expected discover, agent-config, acm-manifests, mount-verify-media, or all)" >&2
    usage
    ;;
esac
