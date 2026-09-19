{
  description = "NixStore — search, install and remove NixOS packages from a TUI";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];

      forAllSystems = fn: nixpkgs.lib.genAttrs supportedSystems (system: fn nixpkgs.legacyPackages.${system});
    in
    {
      # Standalone package (nix build / nix run)
      packages = forAllSystems (pkgs: rec {
        nixstore = pkgs.callPackage ./nix/package.nix { };
        default = nixstore;
      });

      # Overlay — adds pkgs.nixstore to your nixpkgs
      overlays.default = final: prev: {
        nixstore = final.callPackage ./nix/package.nix { };
      };

      # NixOS module — programs.nixstore.*
      nixosModules.default = ./nix/module.nix;
      nixosModules.nixstore = self.nixosModules.default;

      # Development shell — `nix develop`
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ self.packages.${pkgs.stdenv.hostPlatform.system}.default ];
          packages = with pkgs.python3Packages; [ pytest ruff ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
