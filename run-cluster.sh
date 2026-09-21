#!/usr/bin/env bash
# run-cluster.sh - single entry point for running either playbook against
# one cluster, without having to pass -i and -e cluster_dir separately
# every time (they're always derived from the same clusters/<name>/ path).
#
# Usage:
#   ./run-cluster.sh <cluster-name> discover [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> agent-config [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> acm-manifests [-- extra ansible-playbook args]
#   ./run-cluster.sh <cluster-name> all [-- extra ansible-playbook args]
#
# Examples:
#   ./run-cluster.sh ocp-lab discover
#   ./run-cluster.sh ocp-lab agent-config
#   ./run-cluster.sh ocp-lab acm-manifests
#   ./run-cluster.sh ocp-lab all
#   ./run-cluster.sh ocp-lab discover -- --ask-vault-pass
#   ./run-cluster.sh ocp-lab-kvm discover -- -e bmc_password=password
#
# Expects clusters/<cluster-name>/inventory/hosts.yaml to exist - see
# README.md's "Managing multiple clusters" section for the full layout.

set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 <cluster-name> <discover|agent-config|all> [-- extra ansible-playbook args]

Examples:
  $0 ocp-lab discover
  $0 ocp-lab agent-config
  $0 ocp-lab all
  $0 ocp-lab discover -- --ask-vault-pass
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
  all)
    run_discover "$@"
    run_agent_config "$@"
    run_acm_manifests "$@"
    ;;
  *)
    echo "error: unknown action '${ACTION}' (expected discover, agent-config, acm-manifests, or all)" >&2
    usage
    ;;
esac
