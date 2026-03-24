# cephbackup

A simple suite to perform full/incremental backups of KVM virtual machine disk images on CEPH storage, with restore and retention management. Designed for KVM clusters with CEPH storage.

Backup is **crash-consistent**. For application-consistent backups, additional steps (e.g. freezing the VM or saving VRAM) are required before the snapshot.

![Immagine 2025-02-12 155202](https://github.com/user-attachments/assets/f34f4fdb-d8e8-4274-aa51-0538478085a2)

## Stack

- **Python 3** — no external dependencies (stdlib only)
- **CEPH** via `rbd` command
- **KVM/QEMU** via `virsh`
- Configuration via `config.ini` + `backupjobs.json` (no database)

## Files

| File | Purpose |
|------|---------|
| `bkexec.py` | Main script — runs all jobs defined in `backupjobs.json` (cron daily) |
| `bklist.py` | Lists available restore points for a VM (`./bklist.py <vm_name>`) |
| `bkrest.py` | Performs restore (`./bkrest.py <JOB> <IMAGE> <RESTPOINT> <DEST-IMAGE-NAME>`) |
| `bkretention.py` | Applies retention by deleting old backups (cron daily) |
| `bktrimsnap.py` | Deletes obsolete CEPH snapshots (cron daily) |
| `cephbackup_ui.py` | Interactive curses UI for restore management |
| `config.ini` | CEPH pool, libvirt path, email settings |
| `backupjobs.json` | Backup job definitions |
| `TARGETSIDE/` | Scripts to run on the backup target (immutability lock, retention) |

## Prerequisites

- Python 3
- Access to the CEPH pool via `rbd` (configure keyrings on the backup machine)
- A backup target directory mounted before `bkexec.py` starts

## Configuration

**`config.ini`**
```ini
[ceph]
poolname = rbdpool01

[libvirt]
vm_config_path = /etc/libvirt/qemu

[email]
email_from = backup@example.com
rcpt_to    = admin@example.com
```

**`backupjobs.json`**
```json
[
  {
    "name":        "JOBNAME",
    "path":        "/mnt/backup",
    "enabled":     1,
    "max_inc":     5,
    "snap-prefix": "BK",
    "max-snaps":   10,
    "checkmount":  true,
    "mountpoint":  "/mnt/backup",
    "schedule": {
      "days":  [1, 2, 3, 4, 5],
      "weeks": [1, 2, 3, 4, 5]
    },
    "email_from": "backup@example.com",
    "rcpt_to":    ["admin@example.com"]
  }
]
```

## How it works

1. `bkexec.py` scans all enabled jobs in `backupjobs.json`
2. For each job it checks the schedule (days/weeks) — skips if today is not scheduled
3. Checks the lock (`lastrun > lastcompletion`) — if active, notifies by email and exits
4. For each VM directory and each disk image:
   - If never backed up → performs a **FULL** backup
   - Otherwise → performs an **INCREMENTAL** backup until `max_inc` is reached
   - When `max_inc` is reached → rotation: new directory with a new FULL
   - Creates a CEPH snapshot (`rbd snap create`)
   - Runs `rbd export-diff` to the destination directory
   - On failure (non-zero return code or 0-byte file) → removes the file and snapshot
   - On success → copies the VM XML definition to `VMDEF/<indir>/<incset>/`
5. Sends an HTML email report with timing, speed, size and type for each image

## Backup directory structure

```
$job_path/$job_name/
  $vm_name/
    $image_name/
      000001/        ← first full set
        000000       ← FULL
        000001       ← first INC
        000002       ← second INC
      000002/        ← second full set (after rotation)
        000000       ← FULL
    VMDEF/
      000001/000000/
        $vm_name.xml
  LOGS/
    YYYYMMDD_HHMMSS.log
lastbk.txt           ← timestamp of last completed backup
```

## Getting started

1. Copy the scripts to your working directory
2. Create `config.ini` with your CEPH pool, libvirt path and email settings
3. Create `backupjobs.json` with at least one job
4. Create the VM directories under `$job_path/$job_name/`
5. Test with `./bkexec.py` and monitor output/email
6. Add to cron:
   ```
   0 2 * * *  /path/bkexec.py
   0 3 * * *  /path/bkretention.py
   0 4 * * *  /path/bktrimsnap.py
   ```
7. On the target: copy `TARGETSIDE/` and cron `bklock.py`

## Listing restore points

```bash
./bklist.py <vm_name>
```

![Immagine 2025-02-12 155453](https://github.com/user-attachments/assets/065cf3eb-0868-463c-9271-6020800f4c7d)

## Restore

**Command line:**
```bash
./bkrest.py JOB IMAGE RESTPOINT DEST-IMAGE-NAME
```

**Interactive UI:**
```bash
./cephbackup_ui.py
```

The UI guides through VM selection → restore point → image selection → confirmation → live log. The restored VM is defined via `virsh define` and started with networking disabled. To re-enable:
```bash
virsh domif-setlink <vm>_rest-YYYYMMDD <iface> up
```

![Immagine 2025-02-13 083209](https://github.com/user-attachments/assets/7d61b792-b6d8-4b62-bab1-289e84b8829a)

## Retention

Configure `retention` in `backupjobs.json` (number of full sets to keep). Run `bkretention.py` (or cron it) to delete older sets. VMDEF entries for removed backupsets are cleaned up automatically.

## Snapshot trimming

Each backup creates a CEPH snapshot. Run `bktrimsnap.py` to remove old snapshots, keeping the latest `max-snaps` per image.

## TARGETSIDE (backup target)

Scripts to deploy on the backup target server:

| File | Purpose |
|------|---------|
| `bklock.py` | Applies `chattr +i` recursively to all backup files (excludes `vmbackup.json`) |
| `bkretention.py` | Applies retention on the target side |
| `config.ini` | `retention`, `jobname`, `email_from`, `rcpt_to` |

`bklock.py` compares `lastbk.txt` and `lastlock.txt`: applies immutable flags only when new backups are present, then updates `lastlock.txt`.

> **Note:** synchronise the target-side retention threshold with `bklock.py` to ensure immutable flags are removed before deletion.
