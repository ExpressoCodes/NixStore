{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.nixstore;
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    types
    ;

  # The package list lives in the user's own flake, so `nix flake update` never
  # touches it. Until it exists (first rebuild), fall back to initialPackages;
  # the activation script below then creates it with the same contents.
  fileExists = cfg.packagesFile != null && builtins.pathExists cfg.packagesFile;
  installed = if fileExists then lib.importJSON cfg.packagesFile else cfg.initialPackages;

  initialFile = pkgs.writeText "nixstore-packages.json" (builtins.toJSON cfg.initialPackages + "\n");
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
        edits; it must live at `packagesFilePath` on disk. It may not exist yet:
        the first rebuild creates it from `initialPackages`.
      '';
    };

    packagesFilePath = mkOption {
      type = types.str;
      default = "${cfg.flake}/packages.json";
      defaultText = lib.literalExpression ''"''${config.programs.nixstore.flake}/packages.json"'';
      description = "Path of `packagesFile` on disk, which NixStore reads and rewrites.";
    };

    initialPackages = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [
        "git"
        "firefox"
      ];
      description = ''
        Contents for `packagesFilePath` when it does not exist yet. Only used to
        create the file; once it exists it is never overwritten.
      '';
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
    warnings = lib.optional (cfg.packagesFile == null) ''
      programs.nixstore.packagesFile is not set, so packages added with NixStore are
      written to ${cfg.packagesFilePath} but never installed. Set it to e.g. ./packages.json.
    '';

    environment.systemPackages = [
      (cfg.package.override {
        inherit (cfg) flake terminalCommand;
        packagesFile = cfg.packagesFilePath;
      })
    ]
    ++ lib.optionals (cfg.packagesFile != null) (
      map (name: lib.getAttrFromPath (lib.splitString "." name) pkgs) installed
    );

    # First-time setup: create the package list if it is missing. Never overwrites.
    system.activationScripts.nixstore-packages = ''
      if [ ! -e ${lib.escapeShellArg cfg.packagesFilePath} ]; then
        if install -D -m 644 ${initialFile} ${lib.escapeShellArg cfg.packagesFilePath}; then
          echo "nixstore: created ${cfg.packagesFilePath}"
        else
          echo "nixstore: could not create ${cfg.packagesFilePath}" >&2
        fi
      fi
    '';
  };
}
