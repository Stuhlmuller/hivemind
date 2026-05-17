{
  description = "Hivemind agent development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          pythonEnv = pkgs.python312.withPackages (ps: with ps; [
            cryptography
            fastapi
            httpx
            pytest
            uvicorn
            watchfiles
          ]);
          python3Compat = pkgs.writeShellScriptBin "python3" ''
            exec ${pythonEnv}/bin/python "$@"
          '';
          python312Compat = pkgs.writeShellScriptBin "python3.12" ''
            exec ${pythonEnv}/bin/python "$@"
          '';
          hivemindDev = pkgs.writeShellScriptBin "hivemind-dev" ''
            set -euo pipefail

            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            export HIVEMIND_DEVELOPMENT_MODE="''${HIVEMIND_DEVELOPMENT_MODE:-true}"
            export HIVEMIND_DB_PATH="''${HIVEMIND_DB_PATH:-$PWD/.data/hivemind.db}"
            export HIVEMIND_HOST="''${HIVEMIND_HOST:-127.0.0.1}"
            export HIVEMIND_PORT="''${HIVEMIND_PORT:-8000}"

            if [ "$HIVEMIND_DB_PATH" != ":memory:" ]; then
              mkdir -p "$(dirname "$HIVEMIND_DB_PATH")"
            fi

            exec ${pythonEnv}/bin/uvicorn hivemind.api:create_app \
              --factory \
              --reload \
              --host "$HIVEMIND_HOST" \
              --port "$HIVEMIND_PORT"
          '';
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              bash
              coreutils
              findutils
              gh
              git
              gnused
              pythonEnv
              python3Compat
              python312Compat
              hivemindDev
              ripgrep
            ];

            shellHook = ''
              export PATH="${python312Compat}/bin:${python3Compat}/bin:${pythonEnv}/bin:$PATH"
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export HIVEMIND_DB_PATH="''${HIVEMIND_DB_PATH:-$PWD/.data/hivemind.db}"
              export HIVEMIND_HOST="''${HIVEMIND_HOST:-127.0.0.1}"
              export HIVEMIND_PORT="''${HIVEMIND_PORT:-8000}"

              if [ "$HIVEMIND_DB_PATH" != ":memory:" ]; then
                mkdir -p "$(dirname "$HIVEMIND_DB_PATH")"
              fi

              cat <<EOF
Hivemind dev shell ready.
  Start server: hivemind-dev
  Run tests:    pytest
  App URL:      http://$HIVEMIND_HOST:$HIVEMIND_PORT/
  DB path:      $HIVEMIND_DB_PATH
  HTTP auth:    enabled by hivemind-dev only
EOF
            '';
          };
        });
    };
}
