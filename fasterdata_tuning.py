#!/usr/bin/env python3
"""
fasterdata_tuning.py — system tuning helper with safe sysctl updates and optional pacing.

Changes in this version:
- Comments out matching keys in /etc/sysctl.conf before appending new values.
- Adds --pacing flag: sets tc fq maxrate to 20% of the *fastest* active NIC speed.
- Appends the tc command to /etc/rc.local (creating it if needed).
- If a similar tc line exists in rc.local, prints a warning showing existing and proposed lines.
- --dry-run shows all proposed changes (including pacing) without modifying files.
- Exits with error if not Linux or not root (unless --dry-run).
- Always uses fq for the pacing qdisc.
"""

import argparse
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

DEFAULT_SYSCTL = {
    "net.core.rmem_max": "67108864",
    "net.core.wmem_max": "67108864",
    "net.ipv4.tcp_rmem": "4096 87380 33554432",
    "net.ipv4.tcp_wmem": "4096 65536 33554432",
    "net.ipv4.tcp_no_metrics_save": "1",
    "net.ipv4.tcp_mtu_probing": "1",
    "net.core.default_qdisc": "fq",
}

SYSCTL_CONF = "/etc/sysctl.conf"
RC_LOCAL = "/etc/rc.local"

# ---------- Helpers ----------

def require_linux():
    if platform.system() != "Linux":
        print("Error: This script only supports Linux.", file=sys.stderr)
        sys.exit(1)

def require_root(dry_run: bool):
    if not dry_run and os.geteuid() != 0:
        print("Error: Must be run as root unless using --dry-run.", file=sys.stderr)
        sys.exit(1)

def run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        return (0, out, "")
    except subprocess.CalledProcessError as e:
        return (e.returncode, e.output, str(e))
    except FileNotFoundError:
        return (127, "", f"{cmd[0]}: command not found")

def list_net_ifaces() -> List[str]:
    # Use 'ip -o link show' to list interfaces
    rc, out, _ = run_cmd(["ip", "-o", "link", "show"])
    if rc != 0:
        return []
    ifaces = []
    for line in out.splitlines():
        # format: "1: lo: <...> ..."
        m = re.match(r"\d+:\s+([^:]+):", line)
        if m:
            iface = m.group(1)
            # skip loopback, docker, veth, etc.
            if iface.startswith("lo") or iface.startswith("veth") or iface.startswith("docker") or iface.startswith("br-"):
                continue
            ifaces.append(iface)
    return ifaces

def ethtool_speed_mbps(iface: str) -> Optional[int]:
    # parse "Speed: 10000Mb/s" and "Link detected: yes"
    rc, out, _ = run_cmd(["ethtool", iface])
    if rc != 0:
        return None
    link_ok = False
    speed_mbps = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Link detected:"):
            link_ok = line.split(":",1)[1].strip().lower() == "yes"
        elif line.startswith("Speed:"):
            val = line.split(":",1)[1].strip()
            m = re.match(r"(\d+)\s*Mb/s", val, re.IGNORECASE)
            if m:
                speed_mbps = int(m.group(1))
    if link_ok and speed_mbps:
        return speed_mbps
    return None

def pick_fastest_iface() -> Optional[Tuple[str,int]]:
    candidates = []
    for iface in list_net_ifaces():
        sp = ethtool_speed_mbps(iface)
        if sp:
            candidates.append((iface, sp))
    if not candidates:
        return None
    # pick highest speed
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]

def build_tc_fq_maxrate_cmd(iface: str, speed_mbps: int) -> str:
    # 20% of NIC speed, in bits/sec; tc accepts "bit" suffix for bps
    rate_bps = int(speed_mbps * 1_000_000 * 0.20)
    return f"tc qdisc add dev {iface} root fq maxrate {rate_bps}bit"

def file_backup(path: str):
    if not os.path.exists(path):
        return
    bak = f"{path}.bak"
    try:
        shutil.copy2(path, bak)
    except Exception as e:
        print(f"Warning: could not create backup {bak}: {e}", file=sys.stderr)

def comment_out_matching_keys(content: str, keys: List[str]) -> str:
    """
    For each key, find lines like: ^\s*key\s*=\s*.*  (not already commented)
    and prefix with '# '.
    """
    lines = content.splitlines()
    key_patterns = [re.compile(rf"^\s*{re.escape(k)}\s*=", re.IGNORECASE) for k in keys]
    for i, line in enumerate(lines):
        for kp in key_patterns:
            if kp.search(line) and not line.lstrip().startswith("#"):
                lines[i] = "# " + line
                break
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")

def ensure_rc_local_exists(dry_run: bool):
    if os.path.exists(RC_LOCAL):
        return
    text = "#!/bin/sh -e\n\n# Created by fasterdata_tuning.py\n\nexit 0\n"
    if dry_run:
        print(f"[dry-run] Would create {RC_LOCAL} with executable bit and base content.")
        return
    with open(RC_LOCAL, "w", encoding="utf-8") as f:
        f.write(text)
    st = os.stat(RC_LOCAL)
    os.chmod(RC_LOCAL, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def append_tc_to_rc_local(tc_line: str, iface: str, dry_run: bool):
    ensure_rc_local_exists(dry_run=dry_run)
    exists_line = None
    rc_local_text = ""
    if os.path.exists(RC_LOCAL):
        with open(RC_LOCAL, "r", encoding="utf-8", errors="replace") as f:
            rc_local_text = f.read()
        # Look for an existing tc qdisc add dev <iface> root fq ... line
        pattern = re.compile(rf"^\s*tc\s+qdisc\s+add\s+dev\s+{re.escape(iface)}\s+root\s+fq\b.*$", re.MULTILINE)
        m = pattern.search(rc_local_text)
        if m:
            exists_line = m.group(0)

    if exists_line:
        print("WARNING: /etc/rc.local already contains a similar tc line:")
        print(f"  existing: {exists_line}")
        print(f"  proposed: {tc_line}")
        # Do not modify if there is already a line; user can decide.

    else:
        # Append before trailing 'exit 0' if present; else at end.
        new_content = rc_local_text
        if rc_local_text and re.search(r"^\s*exit\s+0\s*$", rc_local_text, re.MULTILINE):
            new_content = re.sub(r"^\s*exit\s+0\s*$", f"{tc_line}\n\nexit 0", rc_local_text, flags=re.MULTILINE)
        else:
            new_content = (rc_local_text.rstrip("\n") + "\n\n" + tc_line + "\n") if rc_local_text else (tc_line + "\n")

        if dry_run:
            print(f"[dry-run] Would append pacing line to {RC_LOCAL}:\n  {tc_line}")
        else:
            file_backup(RC_LOCAL)
            with open(RC_LOCAL, "w", encoding="utf-8") as f:
                f.write(new_content)
            # Ensure executable
            st = os.stat(RC_LOCAL)
            os.chmod(RC_LOCAL, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def update_sysctl_conf(new_settings: Dict[str,str], dry_run: bool):
    # Read current sysctl.conf (empty if missing)
    old = ""
    if os.path.exists(SYSCTL_CONF):
        with open(SYSCTL_CONF, "r", encoding="utf-8", errors="replace") as f:
            old = f.read()

    commented = comment_out_matching_keys(old, list(new_settings.keys()))
    # Append new settings block
    block_lines = ["", "# Added by fasterdata_tuning.py"]
    for k, v in new_settings.items():
        block_lines.append(f"{k} = {v}")
    block = "\n".join(block_lines) + "\n"

    new_content = commented + block if commented else block

    if dry_run:
        print(f"[dry-run] Would update {SYSCTL_CONF}:")
        for k, v in new_settings.items():
            print(f"  set {k} = {v}")
    else:
        file_backup(SYSCTL_CONF)
        with open(SYSCTL_CONF, "w", encoding="utf-8") as f:
            f.write(new_content)

        # Apply immediately
        rc, out, err = run_cmd(["sysctl", "-p", SYSCTL_CONF])
        if rc != 0:
            print(f"Warning: sysctl -p returned {rc}. Output:\n{out}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="Tune sysctl and optionally add pacing via tc fq maxrate (20% of fastest NIC).")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change; do not modify files or run sysctl.")
    parser.add_argument("--pacing", action="store_true", help="Enable pacing: append tc qdisc fq maxrate 20%% of fastest NIC to /etc/rc.local.")
    args = parser.parse_args()

    require_linux()
    require_root(dry_run=args.dry_run)

    # 1) Update sysctl.conf safely (comment out existing keys, then append new values)
    update_sysctl_conf(DEFAULT_SYSCTL, dry_run=args.dry_run)

    # 2) If pacing requested, detect fastest NIC and prepare tc command
    if args.pacing:
        fastest = pick_fastest_iface()
        if not fastest:
            print("Error: could not determine an active interface speed via ethtool.", file=sys.stderr)
            sys.exit(2)
        iface, speed = fastest
        tc_line = build_tc_fq_maxrate_cmd(iface, speed)

        # Show user what we plan to do
        print(f"Fastest interface: {iface} ({speed} Mb/s)")
        print(f"Pacing command (20%): {tc_line}")

        append_tc_to_rc_local(tc_line, iface, dry_run=args.dry_run)

    if args.dry_run:
        print("\n[dry-run] No changes were made.")
    else:
        print("Done.")

if __name__ == "__main__":
    main()
