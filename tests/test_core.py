import json
from pathlib import Path

import pytest

from nixstore import core
from nixstore.core import Config, Package


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    flake = tmp_path / "flake"
    flake.mkdir()
    lock = {
        "nodes": {
            "root": {"inputs": {"nixpkgs": "nixpkgs"}},
            "nixpkgs": {"locked": {"rev": "abc123"}},
        }
    }
    (flake / "flake.lock").write_text(json.dumps(lock))
    (flake / "packages.json").write_text('[\n  "brave-origin",\n  "git"\n]\n')
    return Config(flake=flake, packages_file=flake / "packages.json", cache_dir=tmp_path / "cache")


PACKAGES = [
    Package(a)
    for a in [
        "abtop", "btop", "btop-cuda", "btop-rocm", "usbtop",
        "brave", "brave-origin", "brave-search-cli", "kdePackages.dolphin",
    ]
]


def attrs(packages: list[Package]) -> list[str]:
    return [p.attr for p in packages]


def test_search_ranks_exact_then_prefix_then_substring() -> None:
    assert attrs(core.search(PACKAGES, "btop")) == ["btop", "btop-cuda", "btop-rocm", "abtop", "usbtop"]


def test_search_matches_last_attr_segment() -> None:
    assert attrs(core.search(PACKAGES, "dolphin")) == ["kdePackages.dolphin"]


def test_search_falls_back_to_all_words() -> None:
    assert attrs(core.search(PACKAGES, "brave origin")) == ["brave-origin"]


def test_search_empty_and_no_match() -> None:
    assert core.search(PACKAGES, "  ") == []
    assert core.search(PACKAGES, "vlcc") == []


def test_parse_search_json_strips_prefix_and_skips_library_sets() -> None:
    data = {
        "legacyPackages.x86_64-linux.btop": {"version": "1.4.7", "description": "Monitor\tof\nresources"},
        "legacyPackages.x86_64-linux.python313Packages.requests": {"version": "2", "description": "x"},
        "legacyPackages.x86_64-linux.kdePackages.dolphin": {"version": "25", "description": None},
    }
    assert core.parse_search_json(data) == [
        "btop\t1.4.7\tMonitor of resources\n",
        "kdePackages.dolphin\t25\t\n",
    ]


def test_index_path_uses_pinned_nixpkgs_rev(cfg: Config) -> None:
    assert core.index_path(cfg) == cfg.cache_dir / "abc123.tsv"


def test_read_index(tmp_path: Path) -> None:
    path = tmp_path / "i.tsv"
    path.write_text("btop\t1.4.7\tMonitor of resources\nhello\t\t\n")
    [btop, hello] = core.read_index(path)
    assert (btop.attr, btop.version, btop.description) == ("btop", "1.4.7", "Monitor of resources")
    assert (hello.attr, hello.version) == ("hello", "")


def test_read_installed(cfg: Config) -> None:
    assert core.read_installed(cfg) == {"brave-origin", "git"}
    cfg.packages_file.unlink()
    assert core.read_installed(cfg) == set()


def test_read_system_packages_finds_bare_list_entries(cfg: Config) -> None:
    (cfg.flake / "hyprland.nix").write_text(
        "{ pkgs, ... }: {\n"
        "  environment.systemPackages = with pkgs; [\n"
        "    kitty                  # terminal\n"
        "    kdePackages.dolphin\n"
        "    pkgs.rofi\n"
        "  ];\n"
        "  programs.foo.enable = true;\n"
        "  imports = [ ./boot.nix ];\n"
        "}\n"
    )
    assert core.read_system_packages(cfg) == {
        "kitty": "hyprland.nix",
        "kdePackages.dolphin": "hyprland.nix",
        "rofi": "hyprland.nix",
    }


def test_transaction_files_and_argv(cfg: Config) -> None:
    with core.Transaction(cfg, {"git", "btop"}) as tx:
        assert json.loads(tx.new_file.read_text()) == ["btop", "git"]
        assert tx.old_file.read_text() == cfg.packages_file.read_text()
        argv = tx.argv("-S")
        assert argv[:4] == ["sudo", "-S", "sh", "-c"]
        assert argv[-4:] == [str(tx.new_file), str(cfg.packages_file), str(cfg.flake), str(tx.old_file)]
        tmp = tx.new_file.parent
    assert not tmp.exists()


def test_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NIXSTORE_FLAKE", "/srv/flake")
    monkeypatch.setenv("NIXSTORE_PACKAGES_FILE", "/srv/flake/pkgs/list.json")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.flake == Path("/srv/flake")
    assert cfg.packages_file == Path("/srv/flake/pkgs/list.json")
    assert cfg.cache_dir == tmp_path / "nixstore"
    # --flake overrides both and puts packages.json at its root.
    assert Config.from_env("/other").packages_file == Path("/other/packages.json")
