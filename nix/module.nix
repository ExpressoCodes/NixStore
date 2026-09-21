{
  config,
  lib,
  pkgs,
  inputs ? { },
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

  # ── packages.json ────────────────────────────────────────────────────────────

  # The package list lives in the user's own flake, so `nix flake update` never
  # touches it. Until it exists (first rebuild), fall back to initialPackages;
  # the activation script below then creates it with the same contents.
  fileExists = cfg.packagesFile != null && builtins.pathExists cfg.packagesFile;
  installed = if fileExists then lib.importJSON cfg.packagesFile else cfg.initialPackages;

  initialFile = pkgs.writeText "nixstore-packages.json" (builtins.toJSON cfg.initialPackages + "\n");

  # ── modules.json ─────────────────────────────────────────────────────────────

  modulesFileExists = cfg.modulesFile != null && builtins.pathExists cfg.modulesFile;

  # Parse modules.json at evaluation time; return an empty attrset when absent.
  moduleEntries =
    if modulesFileExists then builtins.fromJSON (builtins.readFile cfg.modulesFile) else { };

  # Build the list of flake-module imports.  Skip entries whose `input` is not
  # present in `inputs` and emit a trace warning so the user knows.
  flakeModuleImports = builtins.concatLists (
    lib.mapAttrsToList (
      name: entry:
      if entry.type or "" == "flake-module" && entry.enabled or false then
        if builtins.hasAttr (entry.input or "") inputs then
          [ inputs.${entry.input}.nixosModules.default ]
        else
          builtins.trace
            "nixstore: skipping module '${name}' — input '${
              entry.input or ""
            }' not found in inputs"
            [ ]
      else
        [ ]
    ) moduleEntries
  );

  # Build a single merged attrset for all program-option entries.
  programOptionAttrs = builtins.foldl' lib.recursiveUpdate { } (
    lib.mapAttrsToList (
      _name: entry:
      if entry.type or "" == "program-option" && entry.enabled or false then
        let
          optPath = lib.splitString "." entry.option;
          parentPath = lib.init optPath;
          baseAttrs = lib.setAttrByPath optPath true;
          extrasAttrs = lib.optionalAttrs (entry ? extras) (lib.setAttrByPath parentPath entry.extras);
        in
        lib.recursiveUpdate baseAttrs extrasAttrs
      else
        { }
    ) moduleEntries
  );
in
{
  # Conditionally import flake modules listed in modules.json.  The list is
  # empty when nixstore is disabled or modulesFile does not exist, so this is
  # a no-op in those cases.
  imports = lib.optionals cfg.enable flakeModuleImports;

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

    modulesFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = lib.literalExpression "./modules.json";
      description = ''
        Path to a JSON file that lists the NixOS modules and program-options
        managed by NixStore.  Each entry is keyed by a module name and carries
        at minimum `type` ("flake-module" or "program-option") and `enabled`.

        Flake-module entries import `inputs.<input>.nixosModules.default` when
        enabled.  Program-option entries set `<option> = true` (plus any
        `extras`) when enabled.

        When the file does not exist the module behaves as if it contained an
        empty object — no extra imports or options are set and evaluation does
        not fail.
      '';
    };

    inputsFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = lib.literalExpression "./nixstore-inputs.nix";
      description = ''
        Path to the Nix file owned by NixStore that declares the flake inputs
        NixStore manages (e.g. nixstore-inputs.nix).  This is informational:
        the Python app reads the rendered path from config to know where to
        write and update that file.
      '';
    };
  };

  config = lib.mkMerge [
    # ── core nixstore config ────────────────────────────────────────────────
    (mkIf cfg.enable {
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
    })

    # ── program-option entries from modules.json ────────────────────────────
    # Applied only when nixstore is enabled and modules.json has content.
    (mkIf (cfg.enable && modulesFileExists) programOptionAttrs)
  ];
}
