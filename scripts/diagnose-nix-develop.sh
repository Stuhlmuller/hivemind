#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
flake_file="$repo_root/flake.nix"
system_cert_link="/etc/ssl/certs/ca-certificates.crt"
system_static_link="/etc/static"
nix_cert_bundle="/nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt"

print_path_state() {
  local path="$1"

  if [[ -L "$path" ]]; then
    printf '[nix-debug] %s -> %s\n' "$path" "$(readlink "$path")"
    if [[ ! -e "$path" ]]; then
      printf '[nix-debug] %s is a broken symlink\n' "$path"
    fi
    return
  fi

  if [[ -e "$path" ]]; then
    printf '[nix-debug] %s exists\n' "$path"
    return
  fi

  printf '[nix-debug] %s is missing\n' "$path"
}

if ! command -v nix >/dev/null 2>&1; then
  echo "[nix-debug] nix is required in PATH" >&2
  exit 1
fi

if [[ ! -f "$flake_file" ]]; then
  echo "[nix-debug] missing flake.nix at $flake_file" >&2
  exit 1
fi

cd "$repo_root"

flake_log="$(mktemp)"
develop_log="$(mktemp)"
trap 'rm -f "$flake_log" "$develop_log"' EXIT

echo "[nix-debug] running nix flake check"
if ! nix flake check >"$flake_log" 2>&1; then
  echo "[nix-debug] nix flake check failed. This looks like a repo or flake problem, not the host-only CA failure this script diagnoses." >&2
  sed -n '1,80p' "$flake_log" >&2
  exit 1
fi

echo "[nix-debug] nix flake check passed"
echo "[nix-debug] running nix develop smoke test"
if nix develop --command bash -lc "printf 'dev-shell-ok\n'" >"$develop_log" 2>&1; then
  echo "[nix-debug] nix develop succeeded"
  sed -n '1,20p' "$develop_log"
  exit 0
fi

echo "[nix-debug] nix develop failed"
sed -n '1,40p' "$develop_log"

print_path_state "$system_cert_link"
print_path_state "$system_static_link"
print_path_state "$nix_cert_bundle"

if grep -q "Problem with the SSL CA cert" "$develop_log"; then
  echo
  echo "[nix-debug] detected an SSL CA failure while entering nix develop"
fi

if [[ -L "$system_cert_link" && ! -e "$system_cert_link" && -f "$nix_cert_bundle" ]]; then
  cat <<EOF

[nix-debug] probable root cause: the default macOS Nix CA symlink is broken.
[nix-debug] repair with:
  sudo rm $system_cert_link
  sudo ln -s $nix_cert_bundle $system_cert_link
  nix develop --command bash -lc "printf 'dev-shell-ok\n'"

[nix-debug] if $system_static_link also points at a missing Nix store path, repair or reinstall the macOS multi-user Nix installation after restoring the cert symlink.
EOF
  exit 2
fi

cat <<EOF

[nix-debug] nix flake check passed, so the repo flake evaluated correctly.
[nix-debug] nix develop is still failing for a machine-local reason. Inspect the paths above, then repair the host Nix daemon installation before retrying.
EOF
exit 2
