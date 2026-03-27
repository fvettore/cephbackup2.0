#!/usr/bin/env python3
"""
bkexec.py — esegue tutti i job di backup definiti in backupjobs.json.
Versione databaseless di bkexec.php. Da mettere in cron daily.

© 2025 — GPLv3
"""

import configparser
import json
import os
import shutil
import smtplib
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


# ─────────────────────────────────────────────────────────── helpers ──

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def day_of_week():
    """Restituisce 1 (lunedì) … 7 (domenica), coerente con schedule.days."""
    return datetime.now().isoweekday()


def week_of_month():
    """Restituisce la settimana del mese (1-5)."""
    return (datetime.now().day - 1) // 7 + 1


def write_atomic(path: Path, content: str):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


# ──────────────────────────────────────────────────── config / state ──

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    return cfg


def load_jobs() -> list:
    with open(SCRIPT_DIR / "backupjobs.json") as f:
        return json.load(f)


def save_jobs(jobs: list):
    write_atomic(SCRIPT_DIR / "backupjobs.json",
                 json.dumps(jobs, indent=2, ensure_ascii=False))


def load_vmbackup(vm_dir: Path) -> dict:
    path = vm_dir / "vmbackup.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"lastrun": None, "success": None}


def save_vmbackup(vm_dir: Path, data: dict):
    write_atomic(vm_dir / "vmbackup.json",
                 json.dumps(data, indent=2))


# ──────────────────────────────────────────────────── backup logic ──

def is_mounted(mountpoint: str) -> bool:
    result = subprocess.run(["mount"], capture_output=True, text=True)
    return mountpoint in result.stdout


def get_vm_images(vm_name, vm_config_path):
    """
    Legge l'XML libvirt e restituisce (lista_immagini, errore).
    Cerca dischi di tipo network/rbd; l'immagine è la parte dopo il '/' nel name.
    """
    xml_path = Path(vm_config_path) / f"{vm_name}.xml"
    if not xml_path.exists():
        return [], f"VM definition not found at {xml_path}"
    try:
        root = ET.parse(xml_path).getroot()
        images = []
        for disk in root.findall(".//disk"):
            src = disk.find("source")
            if src is None:
                continue
            name = src.get("name", "")
            if "/" in name:
                images.append(name.split("/", 1)[1])
        if not images:
            return [], f"No RBD disk images found in {xml_path}"
        return images, None
    except ET.ParseError as e:
        return [], f"XML parse error for {vm_name}: {e}"


def get_backup_state(image_dir):
    """
    Ricava (latest_full_idx, vm_inc) dalla struttura delle cartelle:
      image_dir/000001/000000  ← full
      image_dir/000001/000001  ← primo incrementale
    latest_full_idx = valore numerico dell'ultima cartella di primo livello
    vm_inc          = numero di voci nella cartella più recente (escluso 000000)
    """
    if not image_dir.exists():
        return 0, 0

    full_dirs = sorted(
        d for d in image_dir.iterdir()
        if d.is_dir() and d.name.isdigit()
    )
    if not full_dirs:
        return 0, 0

    latest    = full_dirs[-1]
    latest_full_idx = int(latest.name)
    inc_items = [d for d in latest.iterdir() if d.name.isdigit() and d.name != "000000"]
    vm_inc    = len(inc_items)
    return latest_full_idx, vm_inc


# ──────────────────────────────────────────────────────────── email ──

def send_email(job: dict, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"backup <{job['email_from']}>"
    msg["To"]      = ", ".join(job["rcpt_to"])
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP("localhost") as s:
            for rcpt in job["rcpt_to"]:
                s.sendmail(job["email_from"], rcpt, msg.as_string())
    except Exception as e:
        print(f"WARNING: email send failed: {e}", file=sys.stderr)


def build_report_html(job_name: str, results: list, failed: bool) -> str:
    color  = "red" if failed else "green"
    th = (f'style="padding:12px 8px;text-align:left;'
          f'background-color:{color};color:white;border:1px solid #ddd;"')
    td = 'style="border:1px solid #ddd;padding:8px;"'
    tbl = ('style="font-family:Arial,Helvetica,sans-serif;'
           'border-collapse:collapse;width:100%;"')

    headers = ["VM", "Image", "Start", "End", "Size", "Speed",
               "Status", "Type", "Duration", "Details"]
    html  = f'<table {tbl}><tr>'
    html += "".join(f"<th {th}>{h}</th>" for h in headers)
    html += "</tr>"

    for r in results:
        t1   = datetime.strptime(r["start"], "%Y-%m-%d %H:%M:%S")
        t2   = datetime.strptime(r["end"],   "%Y-%m-%d %H:%M:%S")
        secs = int((t2 - t1).total_seconds())
        dur  = str(t2 - t1)

        sz = r["size"]
        if sz >= 1_000_000_000:
            sz_str = f"{sz / 1e9:.2f} GB"
        elif sz >= 1_000_000:
            sz_str = f"{sz / 1e6:.2f} MB"
        else:
            sz_str = f"{sz} B"

        spd    = f"{int(sz / 1e6 / secs)} MB/s" if secs else "N/A"
        rc_col = "green" if r["result"] == "SUCCESS" else "red"

        html += (
            f'<tr>'
            f'<td {td}>{r["vm"]}</td>'
            f'<td {td}>{r["image"]}</td>'
            f'<td {td}>{r["start"]}</td>'
            f'<td {td}>{r["end"]}</td>'
            f'<td {td}>{sz_str}</td>'
            f'<td {td}>{spd}</td>'
            f'<td {td}><span style="color:{rc_col};">{r["result"]}</span></td>'
            f'<td {td}>{r["type"]}</td>'
            f'<td {td}>{dur} ({secs}s)</td>'
            f'<td {td}>{r["error"]}</td>'
            f'</tr>'
        )
    html += "</table>"
    return html


# ──────────────────────────────────────────────────────── log file ──

def write_log(log_dir, lines):
    log_dir.mkdir(parents=True, exist_ok=True)
    fname = datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
    (log_dir / fname).write_text("\n".join(lines) + "\n")


# ────────────────────────────────────────────────────────── main ──

def main():
    cfg          = load_config()
    pool         = cfg["ceph"]["poolname"]
    vm_cfg_path  = cfg["libvirt"]["vm_config_path"]

    jobs = load_jobs()

    for job_idx, job in enumerate(jobs):
        if not job.get("enabled", 1):
            continue

        job_name = job["name"]
        job_path = job["path"]
        job_dir  = Path(job_path) / job_name
        log_dir  = job_dir / "LOGS"
        lines    = []   # accumula righe di log per questo job

        def lg(msg: str):
            print(msg)
            lines.append(f"[{now_str()}] {msg}")

        lg(f"{'='*60}")
        lg(f"Job: {job_name}")

        # ── schedule check ─────────────────────────────────────────
        sched     = job.get("schedule", {})
        days_ok   = sched.get("days",  list(range(1, 8)))
        weeks_ok  = sched.get("weeks", list(range(1, 6)))
        cur_day   = day_of_week()
        cur_week  = week_of_month()

        if days_ok and cur_day not in days_ok:
            lg(f"Skipping: not scheduled for day {cur_day}")
            write_log(log_dir, lines)
            continue

        if weeks_ok and cur_week not in weeks_ok:
            lg(f"Skipping: not scheduled for week {cur_week}")
            write_log(log_dir, lines)
            continue

        # ── mount check ────────────────────────────────────────────
        if job.get("checkmount"):
            mp = job.get("mountpoint", "")
            lg(f"Checking mountpoint {mp} ...")
            if not is_mounted(mp):
                err = f"Mountpoint {mp} not mounted"
                lg(err)
                send_email(job, f"[FAIL] CEPH Backup {job_name} (ALL objects)",
                           f"<h3>{err}</h3>")
                write_log(log_dir, lines)
                continue
            lg("Mountpoint OK")

        # ── lock check ─────────────────────────────────────────────
        last_run   = job.get("lastrun")
        last_compl = job.get("lastcompletion")
        if last_run and last_compl and last_run > last_compl:
            err = f"Same JOB already running from {last_run}"
            lg(err)
            send_email(job, f"[FAIL] CEPH Backup {job_name} (ALL objects)",
                       f"<h3>{err}</h3>")
            write_log(log_dir, lines)
            continue

        # ── backup starts ──────────────────────────────────────────
        job["lastrun"] = now_str()
        save_jobs(jobs)

        if not job_dir.exists():
            lg(f"ERROR: job directory not found: {job_dir}")
            write_log(log_dir, lines)
            continue

        # Scopre VM scansionando la cartella del job
        SKIP_DIRS = {"LOGS"}
        vm_dirs = sorted(
            d for d in job_dir.iterdir()
            if d.is_dir() and d.name not in SKIP_DIRS
        )

        snap_prefix = job.get("snap-prefix", "BK")
        max_inc     = job.get("max_inc", 5)
        bk_results  = []
        BACKUP_FAIL = False

        for vm_dir in vm_dirs:
            vm_name = vm_dir.name
            lg(f"\n--- VM: {vm_name} ---")

            vmb = load_vmbackup(vm_dir)
            if vmb.get("enabled", 1) == 0:
                lg(f"VM {vm_name} disabled in vmbackup.json, skipping")
                continue

            # Immagini disco dalla definizione libvirt
            images, xml_err = get_vm_images(vm_name, vm_cfg_path)
            if xml_err:
                lg(f"WARNING: {xml_err}")
            if not images:
                # Fallback: scansiona sottocartelle esistenti come nomi immagine
                images = sorted(
                    d.name for d in vm_dir.iterdir()
                    if d.is_dir() and d.name not in {"LOGS", "VMDEF"}
                )
                if not images:
                    lg(f"No images found for {vm_name}, skipping")
                    continue
                lg(f"Fallback: using existing dirs as image names: {images}")

            for vm_image in images:
                image_dir = vm_dir / vm_image
                vm_full, vm_inc = get_backup_state(image_dir)

                # Tipo di backup
                if vm_full == 0:
                    lg(f"FIRST backup for VM {vm_name} image {vm_image}")
                    backup_type = "full"
                    vm_inc  = 0
                    vm_full = 1          # prima cartella: 000001
                elif vm_inc >= max_inc:
                    lg(f"Max INC reached {vm_inc}/{max_inc} for VM {vm_name} — new FULL")
                    backup_type = "full"
                    vm_inc  = 0
                    vm_full += 1         # prossima cartella: indice + 1
                else:
                    lg(f"INC {vm_inc}/{max_inc} for VM {vm_name}")
                    backup_type = "inc"
                    vm_inc += 1

                indir       = str(vm_full).zfill(6)
                incset      = str(vm_inc).zfill(6)
                incset_prev = str(vm_inc - 1).zfill(6)

                backup_dir = image_dir / indir
                backup_dir.mkdir(parents=True, exist_ok=True)

                actual_type = backup_type
                # Se inc ma non esiste il full → esegui full
                if backup_type == "inc" and not (backup_dir / "000000").exists():
                    lg("No previous FULL found, performing FULL instead of INC")
                    actual_type = "full"
                    vm_inc  = 0
                    incset  = "000000"

                # Crea snapshot CEPH
                snap_name        = f"{snap_prefix}-{indir}-{incset}"
                timeout_snap     = job.get("timeout_snap",   120)
                timeout_export   = job.get("timeout_export", 7200)
                cmd_snap  = ["rbd", "snap", "create",
                             f"{pool}/{vm_image}", "--snap", snap_name]
                lg(" ".join(cmd_snap))
                try:
                    subprocess.run(cmd_snap, check=False, timeout=timeout_snap)
                except subprocess.TimeoutExpired:
                    bk_error = fail(f"rbd snap create timed out after {timeout_snap}s")
                    bk_results.append({
                        "vm":     vm_name,
                        "image":  vm_image,
                        "start":  now_str(),
                        "end":    now_str(),
                        "result": "FAIL",
                        "error":  bk_error,
                        "type":   actual_type,
                        "size":   0,
                    })
                    continue

                # Export diff
                if actual_type == "full":
                    cmd_export = [
                        "rbd", "export-diff",
                        f"{pool}/{vm_image}@{snap_name}",
                        str(backup_dir / incset),
                    ]
                else:
                    prev_snap  = f"{snap_prefix}-{indir}-{incset_prev}"
                    cmd_export = [
                        "rbd", "export-diff",
                        "--from-snap", prev_snap,
                        f"{pool}/{vm_image}@{snap_name}",
                        str(backup_dir / incset),
                    ]

                lg(" ".join(cmd_export))
                vm_started = now_str()
                # stderr separato per catturare errori; stdout va diretto al terminale
                try:
                    proc = subprocess.run(cmd_export, stderr=subprocess.PIPE,
                                          text=True, timeout=timeout_export)
                except subprocess.TimeoutExpired:
                    vm_ended = now_str()
                    bk_error = fail(f"rbd export-diff timed out after {timeout_export}s")
                    bk_results.append({
                        "vm":     vm_name,
                        "image":  vm_image,
                        "start":  vm_started,
                        "end":    vm_ended,
                        "result": "FAIL",
                        "error":  bk_error,
                        "type":   actual_type,
                        "size":   0,
                    })
                    continue
                vm_ended   = now_str()

                bk_file = backup_dir / incset

                def cleanup_fail():
                    """Rimuove il file esportato (eventualmente incompleto) e la cartella se vuota."""
                    if bk_file.exists():
                        bk_file.unlink()
                        lg(f"Removed incomplete export file: {bk_file}")
                    try:
                        backup_dir.rmdir()
                        lg(f"Removed empty backup dir: {backup_dir}")
                    except OSError:
                        pass

                def fail(error_msg: str):
                    nonlocal BACKUP_FAIL
                    lg(f"ERROR: {error_msg}")
                    BACKUP_FAIL = True
                    cleanup_fail()
                    cmd_rm = ["rbd", "snap", "rm", f"{pool}/{vm_image}@{snap_name}"]
                    lg(" ".join(cmd_rm))
                    subprocess.run(cmd_rm, check=False)
                    vmb = load_vmbackup(vm_dir)
                    vmb["lastrun"] = vm_ended
                    vmb["success"] = 0
                    save_vmbackup(vm_dir, vmb)
                    return error_msg

                if proc.returncode != 0:
                    err_lines = [l for l in proc.stderr.splitlines() if l.strip()]
                    bk_error  = fail(err_lines[-1] if err_lines else "unknown error")
                    bk_size   = 0
                    res_str   = "FAIL"

                else:
                    bk_size = bk_file.stat().st_size if bk_file.exists() else 0

                    if bk_size == 0:
                        bk_error = fail("export file is empty (0 bytes)")
                        res_str  = "FAIL"

                    else:
                        lg(f"{vm_name} backup SUCCESS ({bk_size} bytes)")
                        res_str  = "SUCCESS"
                        bk_error = ""

                        vmb = load_vmbackup(vm_dir)
                        vmb["lastrun"] = vm_ended
                        vmb["success"] = 1
                        save_vmbackup(vm_dir, vmb)

                        # Copia definizione XML VM in VMDEF (dentro la cartella della VM)
                        vmdef_dir = vm_dir / "VMDEF" / indir / incset
                        xml_src   = Path(vm_cfg_path) / f"{vm_name}.xml"
                        if xml_src.exists():
                            vmdef_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(xml_src, vmdef_dir / f"{vm_name}.xml")
                            lg(f"VM definition copied to {vmdef_dir}/{vm_name}.xml")
                        else:
                            bk_error = f"WARNING: VM definition not found at {xml_src}"
                            lg(bk_error)

                # Riga di log per questa immagine
                lg(f"LOG | job={job_name} vm={vm_name} image={vm_image} "
                   f"type={actual_type} result={res_str} "
                   f"start={vm_started} end={vm_ended} "
                   f"path={backup_dir}/ size={bk_size} "
                   f"cmd={' '.join(cmd_export)}"
                   + (f" error={bk_error}" if bk_error else ""))

                bk_results.append({
                    "vm":     vm_name,
                    "image":  vm_image,
                    "start":  vm_started,
                    "end":    vm_ended,
                    "result": res_str,
                    "error":  bk_error,
                    "type":   actual_type,
                    "size":   bk_size,
                })

        # ── job completato ─────────────────────────────────────────
        job["lastcompletion"] = now_str()
        save_jobs(jobs)

        # Aggiorna lastbk.txt (usato dal TARGETSIDE per l'immutabilità)
        (job_dir / "lastbk.txt").write_text(job["lastcompletion"])

        write_log(log_dir, lines)

        # Email di report
        if bk_results:
            report = "FAIL" if BACKUP_FAIL else "SUCCESS"
            html   = build_report_html(job_name, bk_results, BACKUP_FAIL)
            send_email(job, f"[{report}] CEPH Backup {job_name} ({len(vm_dirs)} objects)", html)
        else:
            send_email(job,
                       f"[FAIL] CEPH Backup {job_name} (ALL objects)",
                       "<h3>No VMs found or all skipped</h3>")


if __name__ == "__main__":
    main()
