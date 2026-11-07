#!/usr/bin/env python3
"""Check and optionally install the host audio dependencies.

CamillaDSP is the pinned official release (camilladsp_release.py). Distribution
packages (apt, dnf, pacman or zypper, from /etc/os-release) are installed only
with ``--install-deps --apply``; a package the configured repositories do not
have (upmpdcli on Debian, Ubuntu and Fedora) is named with where to get it. The installer optionally manages Music
Assistant through Docker Compose, with separate Docker readiness checks.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config/host-stack-manifest.json"


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("schema") != 1 or not isinstance(data.get("executables"), dict)
            or not isinstance(data.get("packages", {}), dict)):
        raise ValueError("unsupported host stack manifest")
    return data


def executable_path(command: str) -> str | None:
    resolved = shutil.which(command)
    if resolved is None:
        return None
    resolved = str(Path(resolved).absolute())
    if any(c.isspace() or c in '%\\"\'$' for c in resolved):
        raise ValueError(f'unsupported executable path: {resolved}')
    return resolved


def missing_executables(manifest: dict, camilladsp=None) -> list[str]:
    manifest = {**manifest, "executables": dict(manifest["executables"])}
    if camilladsp:
        manifest["executables"]["camilladsp"] = str(camilladsp)
    missing = [name for name, command in manifest["executables"].items()
               if shutil.which(command) is None]
    for name in ('upmpdcli',):
        if name in manifest['executables']:
            try:
                executable_path(manifest['executables'][name])
            except ValueError:
                missing.append(name + ':unsupported-executable-path')
    if "camilladsp" in manifest["executables"] and shutil.which(manifest["executables"]["camilladsp"]):
        try:
            command = manifest["executables"]["camilladsp"]
            result = subprocess.run([command, '--version'], capture_output=True, text=True, timeout=5)
            features = subprocess.run([command, '--help'], capture_output=True, text=True, timeout=5)
            if result.returncode or 'CamillaDSP 4.1.3' not in result.stdout or 'pulse-backend' not in features.stdout:
                missing.append('camilladsp:requires-4.1.3-with-pulse-backend')
        except (OSError, subprocess.TimeoutExpired):
            missing.append('camilladsp:requires-4.1.3-with-pulse-backend')
    missing.extend("python:" + name for name in manifest.get("python_modules", [])
                   if importlib.util.find_spec(name) is None)
    for name, minimum in manifest.get("minimum_versions", {}).items():
        command = manifest["executables"].get(name)
        if command and shutil.which(command):
            try:
                out = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            linked = [l for l in out.splitlines() if l.startswith("Linked with")] or [out]
            if version_below(linked[0], minimum):
                missing.append(f"{name}:requires-{minimum}")
    if "mpd" in manifest["executables"] and shutil.which(manifest["executables"]["mpd"]):
        result=subprocess.run([manifest["executables"]["mpd"], "--version"],
                              capture_output=True,text=True,timeout=5)
        outputs=result.stdout.split('Output plugins:',1)[-1].split('Encoder plugins:',1)[0].split()
        if result.returncode or 'pipewire' not in outputs:
            missing.append('mpd:pipewire-output-plugin')
    return missing


# /etc/os-release ID / ID_LIKE -> package manager
FAMILIES = (
    ("apt", {"debian", "ubuntu", "raspbian", "linuxmint", "pop"}),
    ("dnf", {"fedora", "rhel", "centos", "rocky", "almalinux"}),
    ("pacman", {"arch", "manjaro", "endeavouros"}),
    ("zypper", {"suse", "opensuse", "opensuse-tumbleweed", "opensuse-leap", "sles"}),
)
MANAGERS = {"apt": "apt-get", "dnf": "dnf", "pacman": "pacman", "zypper": "zypper"}


def os_release(path: Path = Path("/etc/os-release")) -> dict:
    info = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key.strip()] = value.strip().strip('"\'')
    except OSError:
        pass
    return info


def package_manager(info: dict | None = None, which=shutil.which) -> str | None:
    """apt, dnf, pacman or zypper for this distribution (or whichever is installed)."""
    info = os_release() if info is None else info
    ids = [info.get("ID", "")] + info.get("ID_LIKE", "").split()
    for name in ids:
        for manager, family in FAMILIES:
            if name in family and which(MANAGERS[manager]):
                return manager
    return next((m for m, _ in FAMILIES if which(MANAGERS[m])), None)


def available(manager: str, package: str, run=subprocess.run) -> bool:
    """Whether the configured repositories have the package (no changes)."""
    if manager == "apt":
        out = run(["apt-cache", "policy", package], capture_output=True, text=True).stdout
        candidate = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.strip().startswith("Candidate:")), "")
        return candidate not in ("", "(none)")
    command = {"dnf": ["dnf", "-q", "repoquery", package], "pacman": ["pacman", "-Si", package],
               "zypper": ["zypper", "-q", "info", package]}[manager]
    result = run(command, capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip()) and "not found" not in result.stdout


def install_command(manager: str, packages: list[str], assume_yes: bool) -> list[str]:
    yes = {"apt": ["-y"], "dnf": ["-y"], "pacman": ["--noconfirm"], "zypper": []}[manager] if assume_yes else []
    return {
        "apt": ["apt-get", "install", "--no-install-recommends", *yes, *packages],
        "dnf": ["dnf", "install", "--setopt=install_weak_deps=False", *yes, *packages],
        "pacman": ["pacman", "-S", "--needed", *yes, *packages],
        "zypper": ["zypper", *(["--non-interactive"] if assume_yes else []), "install", "--no-recommends", *packages],
    }[manager]


def install_packages(manifest: dict, apply: bool, assume_yes: bool = False, manager: str | None = None,
                     run=subprocess.run, call=subprocess.call) -> int:
    manager = manager or package_manager()
    packages = manifest.get("packages", {}).get(manager) if manager else None
    if not packages:
        print("No supported package manager (apt, dnf, pacman, zypper); install the packages in "
              "config/host-stack-manifest.json by hand", file=sys.stderr)
        return 2
    sources = manifest.get("package_sources", {})
    # Packages that need a repository the distribution does not ship: only
    # when that repository is already configured; otherwise say where it is.
    elsewhere = [p for p in packages if manager in sources.get(p, {}) and not available(manager, p, run)]
    for package in elsewhere:
        print(f"{package}: not in the configured repositories; add {sources[package][manager]}, "
              "then run --install-deps again", file=sys.stderr)
    command = install_command(manager, [p for p in packages if p not in elsewhere], assume_yes)
    if os.geteuid() != 0:
        command.insert(0, "sudo")
    print("+ " + " ".join(command))
    if not apply:
        return 0
    return call(command)


def version_below(text: str, minimum: str) -> bool:
    import re
    found = re.search(r"(\d+)\.(\d+)", text)
    if not found:
        return True
    return tuple(map(int, found.groups())) < tuple(int(x) for x in minimum.split(".")[:2])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="return failure when an executable is missing")
    parser.add_argument("--install-deps", action="store_true", help="plan/install the distribution packages")
    parser.add_argument("--yes", action="store_true", help="let the package manager install without asking")
    parser.add_argument("--with-share", action="store_true", help="also the packages for the Samba music share")
    parser.add_argument("--apply", action="store_true", help="apply --install-deps; otherwise only print the command")
    parser.add_argument("--prefix", default=str(Path.home() / "hifimate"))
    parser.add_argument("--camilladsp", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    managed = Path(args.prefix) / 'bin/hifimate-camilladsp'
    camilladsp = args.camilladsp or (managed if managed.is_file() else None)
    missing = missing_executables(manifest, camilladsp)
    result = {
        "manifest": str(manifest_path),
        "missing": missing,
        "external_packages": manifest.get("external_packages", []),
        "prefix": args.prefix,
        "package_manager": package_manager(),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if any(item.startswith(("wireplumber:requires", "pipewire:requires")) for item in missing):
        print("PipeWire/WirePlumber too old: Ubuntu 24.04 can use the PipeWire upstream PPAs, older releases need "
              "an upgrade (INSTALL.md, supported distributions)", file=sys.stderr)

    if args.install_deps:
        if args.with_share:
            manager = package_manager()
            extra = manifest.get("share_packages", {}).get(manager or "", [])
            manifest = {**manifest, "packages": {**manifest.get("packages", {}),
                                                 manager: manifest["packages"].get(manager, []) + extra}}
        status = install_packages(manifest, args.apply, args.yes)
        if status:
            return status
        if args.apply:
            missing = missing_executables(manifest, camilladsp)
    return 1 if args.check and missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
