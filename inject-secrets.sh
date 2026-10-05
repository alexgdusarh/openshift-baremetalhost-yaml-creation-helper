#!/usr/bin/env bash
# inject-secrets.sh - write the real pull secret and SSH public key into a
# cluster's already-generated output files, replacing the CHANGE_ME
# placeholders the generators leave behind.
#
# Usage:
#   ./inject-secrets.sh <cluster-name>
#
# Patches, under clusters/<cluster-name>/output/:
#   install-config.yaml   .pullSecret, .sshKey
#   acm/cluster.yaml      every kubernetes.io/dockerconfigjson Secret's
#                         .data[".dockerconfigjson"] (base64), InfraEnv
#                         .spec.sshAuthorizedKey, AgentClusterInstall
#                         .spec.sshPublicKey
#
# The secrets are read from files outside the repo, never from this repo:
#   PULL_SECRET_FILE  default ~/.openshift/pull-secret.txt
#   SSH_KEY_FILE      default ~/.ssh/id_rsa.pub
# Override either one through the environment, e.g.
#   SSH_KEY_FILE=~/.ssh/id_ed25519.pub ./inject-secrets.sh ocp5
#
# Safety checks (the script refuses to write anything if one fails):
#   - every target file must be git-ignored and untracked, so a real secret
#     can never land in a file that gets committed (this is what stops it
#     running against the published ocp-lab / ocp-lab-kvm examples)
#   - the pull secret must be valid JSON with an "auths" key
#   - the SSH key must be a *public* key (catches pointing SSH_KEY_FILE at
#     the private half by mistake)
# Patched files are left mode 600.
#
# Needs mikefarah yq v4 (not the dnf/pip "yq"); set YQ to use a specific binary.

set -euo pipefail
umask 077

usage() {
  echo "Usage: $0 <cluster-name>" >&2
  exit 1
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ $# -eq 1 ]] || usage
cluster="$1"
YQ="${YQ:-yq}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out_dir="$repo_root/clusters/$cluster/output"
[[ -d "$out_dir" ]] || die "$out_dir not found - generate the cluster's output first"

yq_install_hint="install it with:
  curl -sSL -o ~/.local/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 && chmod +x ~/.local/bin/yq
or point YQ at an existing copy: YQ=/path/to/yq $0 $cluster"
command -v "$YQ" >/dev/null || die "yq not found (needs mikefarah yq v4) - $yq_install_hint"
# The dnf/pip "yq" is a different tool (a Python jq wrapper, kislyuk/yq)
# with incompatible syntax.
"$YQ" --version 2>&1 | grep -q 'mikefarah.* v4\.' \
  || die "$(command -v "$YQ") is not mikefarah yq v4 ($("$YQ" --version 2>&1 | head -1)) - $yq_install_hint"

export PULL_SECRET_FILE="${PULL_SECRET_FILE:-$HOME/.openshift/pull-secret.txt}"
export SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_rsa.pub}"

# --- validate the secret sources -------------------------------------------
[[ -r "$PULL_SECRET_FILE" ]] || die "pull secret not readable: $PULL_SECRET_FILE"
[[ -r "$SSH_KEY_FILE" ]] || die "SSH public key not readable: $SSH_KEY_FILE"

case "$PULL_SECRET_FILE" in
  "$repo_root"/*) die "pull secret file must live outside the repo: $PULL_SECRET_FILE" ;;
esac

if [[ $(stat -c '%a' "$PULL_SECRET_FILE") != 600 && $(stat -c '%a' "$PULL_SECRET_FILE") != 400 ]]; then
  echo "WARNING: $PULL_SECRET_FILE is readable by others - run: chmod 600 $PULL_SECRET_FILE" >&2
fi

"$YQ" -e -p json '.auths | length > 0' "$PULL_SECRET_FILE" >/dev/null 2>&1 \
  || die "$PULL_SECRET_FILE is not a pull secret (expected JSON with an \"auths\" key)"

grep -q 'PRIVATE KEY' "$SSH_KEY_FILE" \
  && die "$SSH_KEY_FILE is a PRIVATE key - point SSH_KEY_FILE at the .pub file"
grep -qE '^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-[a-z0-9]+|sk-[a-z0-9@.-]+) AAAA' "$SSH_KEY_FILE" \
  || die "$SSH_KEY_FILE does not look like an SSH public key"

# --- collect targets and make sure git will never pick them up ------------
targets=()
for f in "$out_dir/install-config.yaml" "$out_dir/acm/cluster.yaml"; do
  [[ -f "$f" ]] && targets+=("$f")
done
[[ ${#targets[@]} -gt 0 ]] || die "no install-config.yaml or acm/cluster.yaml under $out_dir"

for f in "${targets[@]}"; do
  rel="${f#"$repo_root"/}"
  if git -C "$repo_root" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
    die "$rel is tracked by git - refusing to write a real secret into it"
  fi
  git -C "$repo_root" check-ignore -q -- "$rel" \
    || die "$rel is not git-ignored - refusing to write a real secret into it"
done

# --- patch -------------------------------------------------------------------
# load_str() reads the files directly, so the secret values never pass
# through the environment or the command line (where ps could see them).
for f in "${targets[@]}"; do
  case "$f" in
    */install-config.yaml)
      "$YQ" -i '
        .pullSecret = (load_str(strenv(PULL_SECRET_FILE)) | trim) |
        .sshKey = (load_str(strenv(SSH_KEY_FILE)) | trim)
      ' "$f"
      ;;
    */acm/cluster.yaml)
      "$YQ" -i '
        with(select(.kind == "Secret" and .type == "kubernetes.io/dockerconfigjson");
          .data[".dockerconfigjson"] = (load_str(strenv(PULL_SECRET_FILE)) | trim | @base64)) |
        with(select(.kind == "InfraEnv");
          .spec.sshAuthorizedKey = (load_str(strenv(SSH_KEY_FILE)) | trim)) |
        with(select(.kind == "AgentClusterInstall");
          .spec.sshPublicKey = (load_str(strenv(SSH_KEY_FILE)) | trim))
      ' "$f"
      ;;
  esac
  chmod 600 "$f"
  # Q0hBTkdFX01F is base64 for "CHANGE_ME" (the Secrets' placeholder)
  grep -nE '(pullSecret|sshKey|dockerconfigjson|sshAuthorizedKey|sshPublicKey).*(CHANGE_ME|Q0hBTkdFX01F)' "$f" >&2 \
    && die "placeholder still present in ${f#"$repo_root"/}"
  echo "patched ${f#"$repo_root"/}"
done
