#!/usr/bin/env python3
"""
fasterdata_tuning.py — system tuning helper with safe sysctl updates, pacing, and NIC tuning.

Features:
- Comments out matching keys in /etc/sysctl.conf before appending new values.
- Adds --pacing flag: sets tc fq maxrate to 20% of fastest or specified NIC.
- Adds --interface option: specify NIC manually.
- Appends tc/ip/ethtool commands to /etc/rc.local (creating it if needed).
- Each appended independently, with timestamped comment lines.
- Duplicate checks with warnings for existing similar lines.
- Prints NIC speed and MTU; warns if MTU < 8000 (suggest 9000).
- Uses "gbit" or "mbit" for tc maxrate; pacing rate ceiled to 100 Mbit increments.
- Includes summary of all actions.
"""

import argparse
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from datetime import date
from typing import Dict, List, Optional, Tuple

# ---------- Constants ----------

TXQUEUELEN_DEFAULT = 10000
RX_RING_DEFAULT = 8192
TX_RING_DEFAULT = 8192

SYSCTL_CONF = "/etc/sysctl.conf"
RC_LOCAL = "/etc/rc.local"

DEFAULT_SYSCTL = {
    "net.core.rmem_max": "67108864",
    "net.core.wmem_max": "67108864",
    "net.ipv4.tcp_rmem": "4096 87380 33554432",
    "net.ipv4.tcp_wmem": "4096 65536 33554432",
    "net.ipv4.tcp_no_metrics_save": "1",
    "net.ipv4.tcp_mtu_probing": "1",
    "net.core.default_qdisc": "fq",
}

# ---------- Utility Functions ----------

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
        return (127, "", f"Command not found: {cmd[0]}")

def list_net_ifaces() -> List[str]:
    rc, out, _ = run_cmd(["ip", "-o", "link", "show"])
    if rc != 0:
        return []
    ifaces = []
    for line in out.splitlines():
        m = re.match(r"\d+:\s+([^:]+):", line)
        if m:
            iface = m.group(1)
            if iface.startswith(("lo", "veth", "docker", "br-", "wg")):
                continue
            ifaces.append(iface)
    return ifaces

def iface_exists(iface: str) -> bool:
    rc, _, _ = run_cmd(["ip", "link", "show", iface])
    return rc == 0

def ethtool_speed_mbps(iface: str) -> Optional[int]:
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

def iface_mtu(iface: str) -> Optional[int]:
    rc, out, _ = run_cmd(["ip", "-o", "link", "show", iface])
    if rc != 0:
        return None
    m = re.search(r"\bmtu\s+(\d+)\b", out)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None

def pick_fastest_iface() -> Optional[Tuple[str,int]]:
    candidates = []
    for iface in list_net_ifaces():
        sp = ethtool_speed_mbps(iface)
        if sp:
            candidates.append((iface, sp))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]

def ceil_100mbit(mbit: float) -> int:
    return int(math.ceil(mbit / 100.0) * 100)

def format_rate_mbit(mbit: int) -> str:
    if mbit >= 1000:
        g = mbit / 1000.0
        s = f"{g:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return f"{s}gbit"
    else:
        return f"{mbit}mbit"

def build_tc_fq_maxrate_cmd(iface: str, speed_mbps: int) -> Tuple[str, int]:
    pacing_mbit = ceil_100mbit(speed_mbps * 0.20)
    rate_str = format_rate_mbit(int(pacing_mbit))
    return (f"tc qdisc add dev {iface} root fq maxrate {rate_str}", int(pacing_mbit))

def file_backup(path: str):
    if not os.path.exists(path):
        return
    bak = f"{path}.bak"
    try:
        shutil.copy2(path, bak)
    except Exception as e:
        print(f"Warning: could not create backup {bak}: {e}", file=sys.stderr)

def comment_out_matching_keys(content: str, keys: List[str]) -> str:
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
        print(f"[dry-run] Would create {RC_LOCAL}.")
        return
    with open(RC_LOCAL, "w", encoding="utf-8") as f:
        f.write(text)
    st = os.stat(RC_LOCAL)
    os.chmod(RC_LOCAL, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def append_line_with_comment(cmd_line: str, comment: str, dry_run: bool):
    ensure_rc_local_exists(dry_run)
    existing_text = ""
    if os.path.exists(RC_LOCAL):
        with open(RC_LOCAL, "r", encoding="utf-8", errors="replace") as f:
            existing_text = f.read()

    pattern = re.compile(rf"^\s*{re.escape(cmd_line.split()[0])}.*{re.escape(cmd_line.split()[2])}.*$", re.MULTILINE)
    m = pattern.search(existing_text)
    if m:
        print(f"WARNING: Similar line already exists in {RC_LOCAL}:")
        print(f"  existing: {m.group(0)}")
        print(f"  proposed: {cmd_line}")
        return

    comment_line = f"# Added by fasterdata_tuning.py – {date.today().isoformat()} – {comment}"
    insertion = f"{comment_line}\n{cmd_line}\n"

    if dry_run:
        print(f"[dry-run] Would append to {RC_LOCAL}:\n  {comment_line}\n  {cmd_line}")
        return

    new_content = existing_text
    if re.search(r"^\s*exit\s+0\s*$", existing_text, re.MULTILINE):
        new_content = re.sub(r"^\s*exit\s+0\s*$", insertion + "\nexit 0", existing_text, flags=re.MULTILINE)
    else:
        new_content = existing_text.rstrip("\n") + "\n" + insertion

    file_backup(RC_LOCAL)
    with open(RC_LOCAL, "w", encoding="utf-8") as f:
        f.write(new_content)

def update_sysctl_conf(new_settings: Dict[str,str], dry_run: bool):
    old = ""
    if os.path.exists(SYSCTL_CONF):
        with open(SYSCTL_CONF, "r", encoding="utf-8", errors="replace") as f:
            old = f.read()
    commented = comment_out_matching_keys(old, list(new_settings.keys()))
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
        rc, out, _ = run_cmd(["sysctl", "-p", SYSCTL_CONF])
        if rc != 0:
            print(f"Warning: sysctl -p returned {rc}.\n{out}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="Tune sysctl and optionally add pacing and NIC tuning.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change; no writes.")
    parser.add_argument("--pacing", action="store_true", help="Enable pacing and NIC tuning.")
    parser.add_argument("--interface", type=str, help="Specify NIC to tune (default: fastest active interface).")
    args = parser.parse_args()

    require_linux()
    require_root(dry_run=args.dry_run)

    update_sysctl_conf(DEFAULT_SYSCTL, dry_run=args.dry_run)
    summary = []

    if args.pacing:
        if args.interface:
            iface = args.interface
            if not iface_exists(iface):
                print(f"Error: interface '{iface}' not found.", file=sys.stderr)
                sys.exit(2)
            speed_mbps = ethtool_speed_mbps(iface)
            if not speed_mbps:
                print(f"Error: unable to detect speed for '{iface}'.", file=sys.stderr)
                sys.exit(2)
        else:
            fastest = pick_fastest_iface()
            if not fastest:
                print("Error: could not detect active NIC via ethtool.", file=sys.stderr)
                sys.exit(2)
            iface, speed_mbps = fastest

        mtu = iface_mtu(iface)
        mtu_str = str(mtu) if mtu is not None else "unknown"
        tc_line, pacing_mbit = build_tc_fq_maxrate_cmd(iface, speed_mbps)

        print(f"Interface: {iface} ({speed_mbps} Mb/s, MTU {mtu_str})")
        if mtu is not None and mtu < 8000:
            print("Warning: MTU below 8000 – consider setting MTU to 9000.")

        print(f"Pacing command: {tc_line}  # ({pacing_mbit} mbit)")

        append_line_with_comment(tc_line, "set pacing", args.dry_run)
        summary.append("✓ Added pacing command to /etc/rc.local")

        ip_line = f"/sbin/ip link set dev {iface} txqueuelen {TXQUEUELEN_DEFAULT}"
        append_line_with_comment(ip_line, "set txqueuelen", args.dry_run)
        summary.append("✓ Added txqueuelen command to /etc/rc.local")

        ethtool_line = f"/usr/sbin/ethtool -G {iface} rx {RX_RING_DEFAULT} tx {TX_RING_DEFAULT}"
        append_line_with_comment(ethtool_line, "set ring buffers", args.dry_run)
        summary.append("✓ Added ring buffer command to /etc/rc.local")

    if args.dry_run:
        print("\n[dry-run] No changes were made.")
    else:
        print("\nSummary:")
        for s in summary:
            print("  " + s)
        print("Done.")

if __name__ == "__main__":
    main()
