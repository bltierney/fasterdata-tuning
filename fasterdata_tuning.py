#!/usr/bin/env python3
"""
fasterdata_tuning.py — System tuning script for high-speed data transfers.

Features:
  • Detects fastest interface and configures sysctl networking parameters
  • Optionally sets fq pacing at 20% of NIC speed
  • Skips duplicate sysctl.conf entries
  • Summarizes all applied settings
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime

SYSCTL_CONF = "/etc/sysctl.conf"
RC_LOCAL = "/etc/rc.local"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd):
    """Run a shell command and return stripped stdout, or None on error."""
    try:
        return subprocess.check_output(cmd, shell=True, text=True).strip()
    except subprocess.CalledProcessError:
        return None


def get_interfaces():
    """Return dict of interface → {'speed': int (Gbps), 'mtu': int}."""
    interfaces = {}
    ip_output = run_cmd("ls /sys/class/net") or ""
    for iface in ip_output.split():
        if iface == "lo":
            continue
        speed = run_cmd(f"ethtool {iface} | grep Speed | awk '{{print $2}}'")
        mtu = run_cmd(f"ip link show {iface} | awk '/mtu/ {{print $5}}'")
        try:
            speed_gbps = int(re.sub(r'[^0-9]', '', speed or '0')) // 1000
        except ValueError:
            speed_gbps = 0
        interfaces[iface] = {"speed": speed_gbps, "mtu": int(mtu or 0)}
    return interfaces


def comment_existing_sysctl(key):
    """Comment out existing sysctl.conf lines that match a key."""
    if not os.path.exists(SYSCTL_CONF):
        return
    with open(SYSCTL_CONF, "r") as f:
        lines = f.readlines()
    changed = False
    with open(SYSCTL_CONF, "w") as f:
        for line in lines:
            if re.match(fr"^\s*{re.escape(key)}\s*=", line):
                f.write(f"# {line.strip()}  # commented by fasterdata_tuning\n")
                changed = True
            else:
                f.write(line)
    if changed:
        print(f"Commented existing sysctl entry for {key}")


def add_sysctl_setting(key, value, summary):
    """Add key=value to sysctl.conf if not already present."""
    existing = ""
    if os.path.exists(SYSCTL_CONF):
        with open(SYSCTL_CONF) as f:
            existing = f.read()
    line = f"{key} = {value}"
    if re.search(fr"^\s*{re.escape(key)}\s*=", existing, re.M):
        print(f"Skipping duplicate sysctl: {key}")
        return
    with open(SYSCTL_CONF, "a") as f:
        f.write(f"{line}\n")
    summary.append(f"Added sysctl: {line}")
    print(f"Added: {line}")


def append_rc_local(cmd_line):
    """Append a command to /etc/rc.local if not already present."""
    if not os.path.exists(RC_LOCAL):
        print(f"WARNING: {RC_LOCAL} does not exist; creating it.")
        with open(RC_LOCAL, "w") as f:
            f.write("#!/bin/sh -e\n\nexit 0\n")
        os.chmod(RC_LOCAL, 0o755)

    with open(RC_LOCAL, "r") as f:
        lines = f.readlines()

    if any(cmd_line in line for line in lines):
        print(f"Command already exists in {RC_LOCAL}")
        return

    insert_pos = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == "exit 0":
            insert_pos = i
            break

    lines.insert(insert_pos, f"{cmd_line}\n")
    with open(RC_LOCAL, "w") as f:
        f.writelines(lines)
    print(f"Appended to {RC_LOCAL}: {cmd_line}")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Tune system for faster data transfer.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes.")
    parser.add_argument("--interface", help="Specify interface; defaults to fastest.")
    parser.add_argument("--pacing", action="store_true", help="Enable fq pacing at 20% of NIC speed.")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("ERROR: Must run as root.")

    # Detect interfaces
    interfaces = get_interfaces()
    if not interfaces:
        sys.exit("No interfaces found.")

    iface = args.interface
    if not iface:
        iface = max(interfaces, key=lambda i: interfaces[i]["speed"])
    info = interfaces[iface]
    speed = info["speed"]
    mtu = info["mtu"]

    print(f"Detected interface: {iface} ({speed} Gbps, MTU {mtu})")

    if not args.pacing:
        print("⚠️  Consider adding '--pacing' to enable fq pacing at 20% of NIC speed.")

    if mtu < 8000:
        print("⚠️  MTU below 9000 detected — consider enabling jumbo frames.")

    summary = [f"Tuning summary ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"]

    sysctl_settings = {
        "net.core.rmem_max": 67108864,
        "net.core.wmem_max": 67108864,
        "net.ipv4.tcp_rmem": "4096 87380 33554432",
        "net.ipv4.tcp_wmem": "4096 65536 33554432",
        "net.ipv4.tcp_no_metrics_save": 1,
        "net.ipv4.tcp_mtu_probing": 1,
        "net.core.default_qdisc": "fq",
    }

    for key, value in sysctl_settings.items():
        comment_existing_sysctl(key)
        add_sysctl_setting(key, value, summary)

    if args.pacing:
        rate_gbps = speed * 0.2
        pacing_cmd = (
            f"/sbin/tc qdisc replace dev {iface} root fq maxrate {rate_gbps:.1f}Gbit"
        )
        if args.dry_run:
            print(f"[DRY RUN] Would apply: {pacing_cmd}")
        else:
            append_rc_local(pacing_cmd)
        summary.append(f"Pacing set to {rate_gbps:.1f} Gbps on {iface}")

    # Increase ring buffer and txqueuelen
    ring_cmds = [
        f"/usr/sbin/ethtool -G {iface} rx 8192 tx 8192",
        f"/sbin/ip link set dev {iface} txqueuelen 10000",
    ]
    for cmd in ring_cmds:
        if args.dry_run:
            print(f"[DRY RUN] Would append: {cmd}")
        else:
            append_rc_local(cmd)
        summary.append(f"Queued {cmd}")

    print("\n--- Summary ---")
    print("\n".join(summary))


if __name__ == "__main__":
    if sys.platform.startswith("linux"):
        main()
    else:
        sys.exit("This script must be run on a Linux system.")

