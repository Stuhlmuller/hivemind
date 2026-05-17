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
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              bash
              coreutils
              curl
              findutils
              gh
              git
              gnused
              lsof
              pythonEnv
              python3Compat
              python312Compat
              ripgrep
            ];

            shellHook = ''
              export PATH="${python312Compat}/bin:${python3Compat}/bin:${pythonEnv}/bin:$PATH"
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export HIVEMIND_DB_PATH="''${HIVEMIND_DB_PATH:-$PWD/.data/hivemind.db}"
            '';
          };
        });
    };
}
