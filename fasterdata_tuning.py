#!/usr/bin/env python3

# This is script to add most of the tuning suggestions from fasterdata.es.net to a Linux host,
# based on NIC speed and MTU size

import argparse
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# ------------------------------
# Utilities
# ------------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)

def need_linux_or_die(quiet: bool = False):
    if platform.system().lower() != "linux":
        msg = "ERROR: This script can only run on Linux hosts."
        if not quiet:
            eprint(msg)
        sys.exit(1)

def run_cmd(cmd: List[str], quiet: bool = False) -> Tuple[int, str, str]:
    """
    Run a command and return (rc, stdout, stderr). Never raise; caller handles rc.
    """
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except Exception as e:
        return 1, "", str(e)

def need_root_or_die(quiet: bool = False):
    if os.geteuid() != 0:
        msg = "ERROR: This script must be run as root."
        if not quiet:
            eprint(msg)
        sys.exit(1)

# ------------------------------
# Host helpers
# ------------------------------

def get_ethernet_interfaces(quiet: bool = False) -> List[str]:
    """
    Use `ip -o link show` to enumerate interfaces. Filter out 'lo'.
    """
    rc, out, err = run_cmd(["ip", "-o", "link", "show"], quiet=quiet)
    if rc != 0:
        raise RuntimeError(f"Failed to list interfaces with ip: {err.strip()}")
    ifaces = []
    for line in out.splitlines():
        # format: "2: ens3: <BROADCAST,MULTICAST,UP,LOWER_UP> ..."
        m = re.match(r"^\d+:\s+([^:]+):", line)
        if m:
            name = m.group(1)
            if name != "lo":
                ifaces.append(name)
    return sorted(set(ifaces))

def get_interface_speed_bps(iface: str, quiet: bool = False) -> Optional[int]:
    """
    Use `ethtool IFACE` to parse 'Speed: 10000Mb/s'. Return bits per second or None if unknown.
    """
    rc, out, err = run_cmd(["ethtool", iface], quiet=quiet)
    if rc != 0:
        # ethtool might not support certain virtual interfaces; treat as unknown
        return None
    m = re.search(r"Speed:\s*([0-9]+)\s*Mb/s", out)
    if not m:
        # Sometimes reports 'Unknown!'
        return None
    try:
        mbps = int(m.group(1))
        # convert Mb/s to bps
        return mbps * 1_000_000
    except ValueError:
        return None

def get_interface_mtu(iface: str, quiet: bool = False) -> Optional[int]:
    """
    Use `ip link show dev IFACE` or sysfs to get MTU.
    """
    rc, out, err = run_cmd(["ip", "link", "show", "dev", iface], quiet=quiet)
    if rc == 0:
        m = re.search(r"mtu\s+(\d+)", out)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    # Fallback to sysfs
    try:
        with open(f"/sys/class/net/{iface}/mtu", "r") as fh:
            return int(fh.read().strip())
    except Exception:
        return None

def get_operating_system_info(quiet: bool = False) -> Dict[str, str]:
    """
    Parse /etc/os-release for distribution_name and distribution_version
    """
    info = {"distribution_name": "", "distribution_version": ""}
    try:
        with open("/etc/os-release", "r") as fh:
            data = fh.read()
        name_m = re.search(r'^NAME="?([^"\n]+)"?', data, flags=re.M)
        ver_m = re.search(r'^VERSION_ID="?([^"\n]+)"?', data, flags=re.M)
        if name_m:
            info["distribution_name"] = name_m.group(1)
        if ver_m:
            info["distribution_version"] = ver_m.group(1)
    except FileNotFoundError:
        # Fallback to platform
        info["distribution_name"] = platform.system()
        info["distribution_version"] = platform.release()
    return info

# ------------------------------
# Core logic 
# ------------------------------

def compute_default_sysctl_settings(max_speed_bps: int, max_mtu: int, os_info: Dict[str, str]) -> Dict[str, str]:
    # Base defaults
    settings: Dict[str, str] = {
        "net.core.rmem_max": "67108864",
        "net.core.wmem_max": "67108864",
        "net.ipv4.tcp_rmem": "4096 87380 33554432",
        "net.ipv4.tcp_wmem": "4096 65536 33554432",
        "net.ipv4.tcp_no_metrics_save": "1",
        "net.core.default_qdisc": "fq",
    }

    # Speed-based overrides
    if max_speed_bps is not None:
        if max_speed_bps >= 40_000_000_000:  # 40G and higher
            settings["net.core.rmem_max"] = "536870912"
            settings["net.core.wmem_max"] = "536870912"
            settings["net.ipv4.tcp_rmem"] = "4096 87380 268435456"
            settings["net.ipv4.tcp_wmem"] = "4096 65536 268435456"
        elif max_speed_bps >= 10_000_000_000:  # 10G and higher
            settings["net.core.rmem_max"] = "268435456"
            settings["net.core.wmem_max"] = "268435456"
            settings["net.ipv4.tcp_rmem"] = "4096 87380 134217728"
            settings["net.ipv4.tcp_wmem"] = "4096 65536 134217728"

    # Jumbo frames consideration
    if max_mtu and max_mtu > 8000:
        settings["net.ipv4.tcp_mtu_probing"] = "1"

    # OS-specific
    dname = os_info.get("distribution_name", "") or ""
    dver = os_info.get("distribution_version", "") or ""
    if re.match(r"^CentOS", dname):
        if re.match(r"^7", dver):
            settings["net.core.default_qdisc"] = "fq"
        elif re.match(r"^6", dver):
            # set if centos 6 and 10Gbps or higher
            if max_speed_bps and max_speed_bps >= 10_000_000_000:
                settings["net.core.netdev_max_backlog"] = "250000"
    elif re.match(r"^Debian", dname):
        if re.match(r"^8", dver):
            settings["net.core.default_qdisc"] = "fq"

    return settings

def append_sysctl_conf(settings: Dict[str, str], dry_run: bool, quiet: bool):
    header = [
        "####################################",
        "#Default sysctl settings",
        "####################################",
    ]
    path = "/etc/sysctl.conf"
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")

    log(f"Preparing to append settings to {path}", quiet)
    if dry_run:
        log("[DRY-RUN] Would append the following lines:", quiet)
        for line in header:
            log(line, quiet)
        for k in sorted(settings.keys()):
            log(f"{k} = {settings[k]}", quiet)
        return

    try:
        with open(path, "a") as fh:
            fh.write("\n".join(header) + "\n")
            for k in sorted(settings.keys()):
                fh.write(f"{k} = {settings[k]}\n")
        log(f"Wrote {len(settings)} settings to {path} at {timestamp}", quiet)
    except Exception as e:
        eprint(f"ERROR: Unable to open/append {path}: {e}")
        sys.exit(1)

def ensure_tc_fq_pacing(iface: str, gbit_rate: float, dry_run: bool, quiet: bool):
    """
    Apply an fq qdisc with maxrate pacing on the interface.
    """
    rate_str = f"{gbit_rate}gbit"
    cmd = ["tc", "qdisc", "replace", "dev", iface, "root", "fq", "maxrate", rate_str]
    if dry_run:
        log(f"[DRY-RUN] Would run: {' '.join(cmd)}", quiet)
        return
    rc, out, err = run_cmd(cmd, quiet=quiet)
    if rc != 0:
        eprint(f"WARNING: tc pacing failed on {iface}: {err.strip()}")
    else:
        log(f"Applied fq maxrate {rate_str} on {iface}", quiet)

# ------------------------------
# CLI and main
# ------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Configure sysctl defaults (perfSONAR-style) and optional tc pacing."
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would happen, but do not change anything.")
    p.add_argument("--quiet", action="store_true", help="Suppress output.")
    p.add_argument("--pacing", type=float, metavar="N", help="Enable tc fq maxrate pacing at N Gbps on all non-loopback interfaces.")
    return p.parse_args()

def main():
    args = parse_args()

    # Root check (always required, even for dry-run per request)
    need_root_or_die(quiet=args.quiet)
    need_linux_or_die(quiet=args.quiet)

    log("Starting configure_sysctl (Python translation)", args.quiet)

    # Discover interfaces, max speed, and max MTU
    ifaces = get_ethernet_interfaces(quiet=args.quiet)
    log(f"Discovered interfaces: {', '.join(ifaces) if ifaces else '(none)'}", args.quiet)

    max_speed = 0
    max_mtu = 0

    for iface in ifaces:
        spd = get_interface_speed_bps(iface, quiet=args.quiet)
        mtu = get_interface_mtu(iface, quiet=args.quiet)
        log(f"Interface {iface}: speed={spd if spd is not None else 'unknown'} bps, mtu={mtu if mtu else 'unknown'}", args.quiet)
        if spd and spd > max_speed:
            max_speed = spd
        if mtu and mtu > max_mtu:
            max_mtu = mtu

    log(f"Max detected speed: {max_speed if max_speed else 'unknown'} bps", args.quiet)
    log(f"Max detected MTU: {max_mtu if max_mtu else 'unknown'}", args.quiet)

    os_info = get_operating_system_info(quiet=args.quiet)
    log(f"OS detected: {os_info.get('distribution_name','')} {os_info.get('distribution_version','')}", args.quiet)

    settings = compute_default_sysctl_settings(max_speed, max_mtu, os_info)

    # Show computed settings
    log("Computed sysctl settings:", args.quiet)
    for k in sorted(settings.keys()):
        log(f"  {k} = {settings[k]}", args.quiet)

    # Append to /etc/sysctl.conf (or dry-run print)
    append_sysctl_conf(settings, dry_run=args.dry_run, quiet=args.quiet)

    # Optional pacing
    if args.pacing is not None:
        log(f"Applying tc fq pacing at {args.pacing} Gbps on all non-loopback interfaces", args.quiet)
        for iface in ifaces:
            ensure_tc_fq_pacing(iface, args.pacing, dry_run=args.dry_run, quiet=args.quiet)

    log("Done.", args.quiet)
    return 0

if __name__ == "__main__":
    sys.exit(main())
