# cephbackup2.0

A simple suite to perform full/incremental backups of KVM virtual machine disk images on CEPH storage, with restore and retention management. Designed for KVM clusters with CEPH storage.

Backup is **crash-consistent**. For application-consistent backups, additional steps (e.g. freezing the VM or saving VRAM) are required before the snapshot.

2.0 version has fully functional UI


![Immagine 2026-03-24 141028](https://github.com/user-attachments/assets/597ed3c6-cfdb-40a6-9f78-f5746f4bc97f)

Every scheduled action proiduces a detailed email reporting relevant data:

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
| `bkcrypt.py` | Optional backup encryption helpers (gpg), used by the other scripts |
| `bkstate.py` | Backup/restore of UEFI NVRAM and vTPM state of the VMs (paths read from the VM XML) |
| `bksiem.py` | Local log for a SIEM (`/var/log/cephbackup/siem.log`, one `key=value` line per event) |
| `bkcheckkey.py` | Verifies that an encryption key opens the backups of a job (`./bkcheckkey.py JOB [VM] [--full] [--ask]`) |
| `bkwatchdog.py` | Emails an alert when an enabled job has not completed for more than `alert_hours` (default 26) — catches hung runs (cron daily, after the backup window) |
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

; optional section (comments on their own lines only)
[vmstate]
; 0 = do not save NVRAM/TPM
backup            = 1
; where the UI creates the NVRAM of restored VMs (empty = same dir as the original)
nvram_restore_dir =
; where the UI creates the TPM dirs of restored VMs (empty = same dir as the original)
tpm_restore_dir   =
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
    "parallel":    1,
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
   - If `vmbackup.json` contains `"enabled": 0` → **VM is skipped**
   - If never backed up → performs a **FULL** backup
   - Otherwise → performs an **INCREMENTAL** backup until `max_inc` is reached
   - When `max_inc` is reached → rotation: new directory with a new FULL
   - Creates a CEPH snapshot (`rbd snap create`)
   - Runs `rbd export-diff` to the destination directory
   - On failure (non-zero return code or 0-byte file) → removes the file and snapshot
   - On success → copies the VM XML definition to `VMDEF/<indir>/<incset>/`
5. Sends an HTML email report with timing, speed, size and type for each image, plus a **TOTAL** row: total size, job duration (start to end, wall clock) and average speed (total size / job duration)

### Parallel images

`"parallel": N` (1–5, default 1) backs up up to N disk images of the same job at the same time (snapshot + `export-diff` [+ `gpg`] per image). Useful when a single stream does not saturate the link or the target. With N > 1 every log line of an image is prefixed with `[VM/image]`; the report keeps the VM order. Writes to the shared files of a VM (`vmbackup.json`, `VMDEF/`) are serialized, so multi-disk VMs are safe.

See [NFS target and `nconnect`](#nfs-target-and-nconnect) to let parallel streams use more than one TCP connection.

### NFS target and `nconnect`

By default an NFS mount uses **one TCP connection** to the server: all parallel streams share it, and with a bond they all hash to the same link. The `nconnect=N` mount option (Linux ≥ 5.3, N up to 16) opens N connections to the same server and spreads the traffic across them.

`/etc/fstab` example:
```
nas01:/export/backup  /mnt/backup  nfs4  hard,_netdev,nofail,nconnect=2  0 0
```

Apply it with a full unmount/mount; `mount -o remount` does **not** change `nconnect`. Do it outside the backup window:
```bash
systemctl daemon-reload
umount /mnt/backup && mount /mnt/backup
grep /mnt/backup /proc/mounts        # must show nconnect=2
ss -tn dst <nas-ip>:2049             # one ESTAB line per connection
```

Notes:
- `nconnect` is per client–server pair: if several exports of the same server are mounted, the first mount sets it for all of them. Put the same value on every line for that server and unmount them all before remounting
- It only helps if there are several streams: set `parallel` > 1 in the job (e.g. `parallel` = `nconnect`)
- Bond mode matters: with `802.3ad` (LACP) and `xmit_hash_policy=layer3+4` different connections can use different links; with `active-backup` only one link is active, so `nconnect` helps only when the single connection (not the link) is the bottleneck
- Larger `rsize`/`wsize` (e.g. `1048576`) usually help with big sequential writes; check the value actually negotiated in `/proc/mounts`
- Compare the **TOTAL** row of the email report (speed = total size / job duration) before and after the change, using runs of the same type (incremental vs incremental)

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
   0 4 * * *  /path/bktrimsnap.py
   0 8 * * *  /path/bkwatchdog.py
   ```
7. On the target: copy `TARGETSIDE/` and cron `bklock.py` and `bkretention.py`

## UEFI NVRAM and vTPM state

VMs with UEFI/Secure Boot and a virtual TPM keep state outside the disks: the NVRAM file (`<os><nvram>`) and the swtpm directory (`<tpm><backend type='emulator'><source type='dir' path=.../>`). Without them a full restore loses boot entries and updated Secure Boot keys, and BitLocker asks for the recovery key.

After each successful disk backup `bkexec.py` saves them next to the XML:
```
VMDEF/<set>/<point>/<vm>.xml
VMDEF/<set>/<point>/<vm>_VARS.fd            NVRAM (plain)
VMDEF/<set>/<point>/TPM/tpm2-00.permall     TPM state (.gpg when the job is encrypted)
```
- Paths are always read from the VM XML; VMs without `<nvram>`/`<tpm>` are skipped
- Files are read while the VM runs: two identical reads with unchanged mtime are required
- A problem with NVRAM/TPM is a WARNING in the report, never a failed disk backup
- `bklist.py` and the UI show an `NT` column (`N` = NVRAM, `T` = TPM, `-` = missing)

Restore:
- **UI**: NVRAM and TPM of the restore point are copied to **new** files (`<vm>_rest-YYYYMMDD_VARS.fd`, `<vm>_rest-YYYYMMDD/`) and the restored XML points to them — never to the production VM files (its swtpm holds a lock on the TPM directory). Owner/mode: `qemu:qemu 600` for the NVRAM, `tss:tss 700/600` for the TPM. If the restore point has no state, libvirt creates a new NVRAM from the template and swtpm a new empty TPM
- **`bkrest.py ... --state`** also restores NVRAM/TPM onto the **original** VM, **`bkrest.py JOB VM RESTPOINT --state-only`** restores only them. The VM must be shut off (checked with Pacemaker `crm_resource --locate` for resource `VM_<name>`, then `virsh domstate`); current files are renamed `.bak-<date>`
- Restoring only an old disk of a UEFI VM: if Windows updated the Secure Boot revocation list (DBX) since then, the old boot manager may not start with the current NVRAM — restore the NVRAM of the same point too

## Listing restore points

```bash
./bklist.py <vm_name>
```

![Immagine 2025-02-12 155453](https://github.com/user-attachments/assets/065cf3eb-0868-463c-9271-6020800f4c7d)

## Restore

**Command line:**
```bash
./bkrest.py JOB IMAGE RESTPOINT DEST-IMAGE-NAME [--state]
./bkrest.py JOB VM RESTPOINT --state-only
```

**Interactive UI:**
```bash
./cephbackup_ui.py
```

The UI provides two sections from the main menu:

**Backup management** — select a job to access:
- *Edit*: configure job parameters (path, schedule, email, etc.)
- *VM management*: list VMs with enabled/disabled status; press `Enter`/`Space` to toggle backup on/off for a VM; `a` or `[+ Add VM]` to register a new VM (creates the directory and `vmbackup.json`)

**Restore** — guides through VM selection → restore point → image selection → confirmation → live log. The restored VM is defined via `virsh define` and started with the network interface present but link-down. To re-enable:
```bash
virsh domif-setlink <vm>_rest-YYYYMMDD <iface> up
```

![Immagine 2025-02-13 083209](https://github.com/user-attachments/assets/7d61b792-b6d8-4b62-bab1-289e84b8829a)

## Encryption (optional)

Backups can be encrypted per job with `gpg` (AES256, symmetric passphrase). Enable it in `backupjobs.json` (or from the UI job editor):

```json
"encrypt": 1,
"encryption_key_file": "/root/.cephbackup/keys/JOBNAME.key"
```

Create the key file (first line = passphrase) and keep a copy **outside the cluster** — without it encrypted backups cannot be restored:
```bash
install -d -m 700 /root/.cephbackup/keys
openssl rand -base64 48 > /root/.cephbackup/keys/JOBNAME.key && chmod 600 /root/.cephbackup/keys/JOBNAME.key
```

- `rbd export-diff` is piped straight into `gpg`: no plaintext ever reaches the target
- Encrypted files get the `.gpg` suffix (`000003.gpg`); plain files keep the bare name (`000003`)
- **Mixed chains** are supported: enabling encryption on an existing job only encrypts new files; after a full retention cycle the job is fully encrypted
- If `encrypt` is on and the key file is missing, the job fails (email) — it never falls back to plaintext
- Restore (UI and `bkrest.py`) uses the key registered in the job; if none is registered (or it does not match) the key is asked interactively and checked before anything is created
- `bklist.py` and the UI mark encrypted restore points with `*`
- VM XML definitions in `VMDEF/` are not encrypted
- From the UI (*Backup → job → Encryption*) you can toggle encryption, enter or generate the key (saved as a 600 file and registered in the job) and verify it against the latest backups. A new key is refused if it does not open the job's existing encrypted files

Verify a key against the backups of a job:
```bash
./bkcheckkey.py JOB [VM]          # quick: decrypts the header of the latest file of each image
./bkcheckkey.py JOB --full        # decrypts every file of the latest set (integrity check, slow)
./bkcheckkey.py JOB --ask         # ask for the key even if one is registered (e.g. test the offline copy)
```

## SIEM log

Besides the email and the logs on the target, every script writes one line per event to a **local** file, meant to be collected by a log shipper (Fluent Bit, Filebeat, ...). It is never on NFS/CephFS, so a hung mount cannot block the collector:

```
/var/log/cephbackup/siem.log      (dir 0750, file 0640, rotated by contrib/logrotate-cephbackup)
2026-11-19T21:37:38+01:00 cephbackup event=image job=JOB vm=VM image=IMG type=full result=SUCCESS start=... end=... size=3122690128 encrypted=0
```

- Events: `job_start`, `job_skip`, `job_abort` (`reason=mount|lock|encryption|novm`), `image`, `total`, `snap_remove`, `restore_start` / `restore_end` (with `user=`), `watchdog_alert`, `config_change` (UI edits of `backupjobs.json` and VM enable/disable)
- Timestamps with timezone; values with spaces are double-quoted; max 500 characters per value; no secrets (no passphrases or key file paths)
- Writing the line can never fail or slow down a backup: errors are ignored
- Test: `./bkexec.py --siem-test` or `./bksiem.py --test`; the path can be changed with the `CEPHBACKUP_SIEM_LOG` environment variable
- Install the rotation: `cp contrib/logrotate-cephbackup /etc/logrotate.d/cephbackup`
- `TARGETSIDE/bklock.py` and `TARGETSIDE/bkretention.py` send `event=target_lock` / `event=target_retention_delete` to the target's syslog (`logger -t siem-cephbackup -p authpriv.notice`)

## Retention

Configure `retention` in `backupjobs.json` (number of full sets to keep). Run `bkretention.py` (or cron it) to delete older sets. VMDEF entries for removed backupsets are cleaned up automatically.

## Snapshot trimming

Each backup creates a CEPH snapshot. Run `bktrimsnap.py` to remove old snapshots, keeping the latest `max-snaps` per image.

## TARGETSIDE (backup target)

Scripts to deploy on the backup target server:

| File | Purpose |
|------|---------|
| `bklock.py` | Applies `chattr +i` recursively to all backup files (excludes `vmbackup.json` and `VMDEF/`, which stay writable) |
| `bkretention.py` | Applies retention on the target side |
| `config.ini` | `retention`, `jobname`, `email_from`, `rcpt_to` |

`bklock.py` compares `lastbk.txt` and `lastlock.txt`: applies immutable flags only when new backups are present, then updates `lastlock.txt`.

> **Note:** synchronise the target-side retention threshold with `bklock.py` to ensure immutable flags are removed before deletion.
