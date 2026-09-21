{
  lib,
  python3Packages,
  libnotify,

  # Optional overrides (the NixOS module sets these):
  # Command that runs a program in a terminal window, e.g.
  # "kitty --class nixstore --title NixStore". null keeps Terminal=true in the
  # desktop entry and lets the desktop environment pick the terminal.
  terminalCommand ? null,
  # Defaults baked into the wrapper; NIXSTORE_FLAKE / NIXSTORE_PACKAGES_FILE
  # in the environment still take precedence.
  flake ? null,
  packagesFile ? null,
  dotfilesDir ? null,
}:

python3Packages.buildPythonApplication {
  pname = "nixstore";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../nixstore
      ../tests
      ../data
    ];
  };

  build-system = [ python3Packages.hatchling ];
  dependencies = [ python3Packages.textual ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  makeWrapperArgs =
    [
      "--prefix" "PATH" ":" (lib.makeBinPath [ libnotify ])
      "--set" "NIXSTORE_UPDATE_SCRIPT" "${placeholder "out"}/share/nixstore/update.sh"
    ]
    ++ lib.optionals (flake != null) [ "--set-default" "NIXSTORE_FLAKE" flake ]
    ++ lib.optionals (packagesFile != null) [ "--set-default" "NIXSTORE_PACKAGES_FILE" packagesFile ]
    ++ lib.optionals (dotfilesDir != null) [ "--set-default" "NIXSTORE_DOTFILES" dotfilesDir ];

  postInstall =
    ''
      install -Dm644 data/nixstore.desktop $out/share/applications/nixstore.desktop
      install -Dm755 data/update.sh $out/share/nixstore/update.sh
      for icon in data/icons/hicolor/*/apps/nixstore.png; do
        install -Dm644 "$icon" "$out/share/icons/''${icon#data/icons/}"
      done
    ''
    + (
      if terminalCommand == null then
        ''
          substituteInPlace $out/share/applications/nixstore.desktop \
            --replace-fail "Exec=nixstore" "Exec=$out/bin/nixstore"
        ''
      else
        ''
          substituteInPlace $out/share/applications/nixstore.desktop \
            --replace-fail "Exec=nixstore" "Exec=${terminalCommand} $out/bin/nixstore" \
            --replace-fail "Terminal=true" "Terminal=false"
        ''
    );

  meta = {
    description = "Search, install and remove NixOS packages from a TUI";
    homepage = "https://github.com/ExpressoCodes/NixStore";
    license = lib.licenses.mit;
    mainProgram = "nixstore";
    platforms = lib.platforms.linux;
  };
}
