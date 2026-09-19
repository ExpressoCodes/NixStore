{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.nixstore;
  inherit (lib) mkEnableOption mkIf mkOption types;
in
{
  options.programs.nixstore = {
    enable = mkEnableOption "NixStore, a TUI to search, install and remove packages";

    package = mkOption {
      type = types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "nixstore from this flake";
      description = "The NixStore package.";
    };

    flake = mkOption {
      type = types.str;
      default = "/etc/nixos";
      description = "Directory of the system flake NixStore edits and rebuilds.";
    };

    packagesFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = lib.literalExpression "./packages.json";
      description = ''
        JSON list of nixpkgs attribute names (e.g. `[ "git" "kdePackages.dolphin" ]`)
        that is added to `environment.systemPackages`. This is the file NixStore
        edits; it must live at `packagesFilePath` on disk.
      '';
    };

    packagesFilePath = mkOption {
      type = types.str;
      default = "${cfg.flake}/packages.json";
      defaultText = lib.literalExpression ''"''${config.programs.nixstore.flake}/packages.json"'';
      description = "Path of `packagesFile` on disk, which NixStore reads and rewrites.";
    };

    terminalCommand = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = lib.literalExpression ''"''${pkgs.kitty}/bin/kitty --class nixstore --title NixStore"'';
      description = ''
        Command that opens a terminal running the program appended to it; used by
        the desktop entry. null uses `Terminal=true` and your desktop's default terminal.
      '';
    };
  };

  config = mkIf cfg.enable {
    environment.systemPackages =
      [
        (cfg.package.override {
          inherit (cfg) flake terminalCommand;
          packagesFile = cfg.packagesFilePath;
        })
      ]
      ++ lib.optionals (cfg.packagesFile != null) (
        map (name: lib.getAttrFromPath (lib.splitString "." name) pkgs) (lib.importJSON cfg.packagesFile)
      );
  };
}
