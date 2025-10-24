# fasterdata-tuning

**Linux Host Network Tuning**  
*Based on the recommendations from [fasterdata.es.net](https://fasterdata.es.net)*

---

## Overview

This Python script can be used to optimize your Linux host for **high-speed networking**, following the tuning guidelines from [fasterdata.es.net](https://fasterdata.es.net).  
These settings are primarily intended for systems connected at **10 Gbps or higher**.

---

## Features

The script performs the following tuning actions:

- **System Configuration (`sysctl.conf`)**  
  See: [Linux Test & Measurement Host Tuning](https://fasterdata.es.net/host-tuning/linux/test-measurement-host-tuning/)

- **Optional TCP Pacing**  
  See: [Linux Packet Pacing](https://fasterdata.es.net/host-tuning/linux/packet-pacing/)

- **Additional NIC-specific Tuning**  
  See: [100 G Linux Tuning](https://fasterdata.es.net/host-tuning/linux/100g-tuning/)
  - Ring Buffer tuning  
  - Pause Frame verification

---

## Future Enhancements

- **Mellanox ConnectX-7 tuning:**  
  [NIC Device Driver Tuning Guide](https://fasterdata.es.net/host-tuning/linux/100g-tuning/nic-device-driver/)

---

## Notes

- Always review system-specific requirements before applying settings.
- Run the script with administrative privileges (`sudo`) for configuration changes.
- Use the `--dry-run` option to preview changes before applying them.

