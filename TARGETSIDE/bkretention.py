#!/usr/bin/env python3
"""
bkretention.py — cancella le cartelle di backup più vecchie lato TARGET.
Il numero massimo di backup FULL da conservare è indicato in config.ini.
Da mettere in cron.

© 2025 — GPLv3
"""

import configparser
import shutil
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent
SKIP_DIRS  = {"VMDEF", "LOGS"}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    retention = cfg.getint("job",   "retention", fallback=2)
    jobname   = cfg.get("job",      "jobname",   fallback="")
    email_from = cfg.get("email",   "email_from", fallback="")
    rcpt_to   = [r.strip() for r in
                 cfg.get("email", "rcpt_to", fallback="").split(",") if r.strip()]
    return {
        "retention":  retention,
        "jobname":    jobname,
        "email_from": email_from,
        "rcpt_to":    rcpt_to,
    }


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(cfg: dict, subject: str, html_body: str):
    if not cfg["rcpt_to"]:
        print("WARNING: nessun destinatario email configurato, email non inviata.",
              file=sys.stderr)
        return
    print(f"Invio email a {cfg['rcpt_to']} ...")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"backup <{cfg['email_from']}>"
    msg["To"]      = ", ".join(cfg["rcpt_to"])
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP("localhost") as s:
            for rcpt in cfg["rcpt_to"]:
                s.sendmail(cfg["email_from"], rcpt, msg.as_string())
        print("Email inviata.")
    except Exception as e:
        print(f"WARNING: email send failed: {e}", file=sys.stderr)


def build_report_html(jobname: str, purged: list, stale: list) -> str:
    th_blue = ('style="padding:12px 8px;text-align:left;'
               'background-color:blue;color:white;border:1px solid #ddd;"')
    th_org  = ('style="padding:12px 8px;text-align:left;'
               'background-color:#e67e00;color:white;border:1px solid #ddd;"')
    td  = 'style="border:1px solid #ddd;padding:8px;"'
    tbl = ('style="font-family:Arial,Helvetica,sans-serif;'
           'border-collapse:collapse;width:100%;margin-bottom:24px;"')

    html = ""

    if purged:
        html += (f"<h3>Cartelle di backup eliminate</h3>"
                 f'<table {tbl}><tr>'
                 f'<th {th_blue}>JOB</th><th {th_blue}>PATH</th>'
                 f'<th {th_blue}>RESULT</th></tr>')
        for path, result in purged:
            color = "green" if result == "SUCCESS" else "red"
            html += (f'<tr><td {td}>{jobname}</td><td {td}>{path}</td>'
                     f'<td {td}><span style="color:{color};">{result}</span></td>'
                     f'</tr>')
        html += "</table>"

    if stale:
        html += (f"<h3>&#9888; Immagini senza backup da oltre 3 mesi</h3>"
                 f'<table {tbl}><tr>'
                 f'<th {th_org}>VM</th><th {th_org}>IMMAGINE</th>'
                 f'<th {th_org}>ULTIMO BACKUP</th><th {th_org}>PATH</th>'
                 f'</tr>')
        for vm, image, last_date, path in stale:
            html += (f'<tr><td {td}>{vm}</td><td {td}>{image}</td>'
                     f'<td {td}><span style="color:#e67e00;font-weight:bold;">'
                     f'{last_date}</span></td><td {td}>{path}</td></tr>')
        html += "</table>"

    return html


# ── Retention logic ───────────────────────────────────────────────────────────

def chattr_remove(path: Path):
    """Rimuove il flag immutable ricorsivamente da una cartella."""
    subprocess.run(["chattr", "-iR", str(path)], check=False)


def purge_backupset(bkset_dir: Path) -> str:
    """
    Rimuove il flag immutable e cancella la cartella del backupset.
    Ritorna 'SUCCESS' o 'FAIL'.
    """
    print(f"    removing immutable flags from {bkset_dir}")
    chattr_remove(bkset_dir)

    print(f"    deleting {bkset_dir}")
    try:
        shutil.rmtree(bkset_dir)
    except Exception as e:
        print(f"    ERROR: {e}")
        return "FAIL"

    if bkset_dir.exists():
        print(f"    ERROR: directory still exists after rm")
        return "FAIL"

    return "SUCCESS"


def latest_mtime(image_dir: Path) -> Optional[datetime]:
    """Ritorna il datetime dell'ultimo file di backup nell'image_dir, o None."""
    latest = None
    for bkset in image_dir.iterdir():
        if not bkset.is_dir() or not bkset.name.isdigit():
            continue
        for f in bkset.iterdir():
            if f.is_file() and f.name.isdigit():
                mt = datetime.fromtimestamp(f.stat().st_mtime)
                if latest is None or mt > latest:
                    latest = mt
    return latest


def find_stale_images(stale_days: int = 90) -> list:
    """
    Rileva immagini il cui ultimo backup è più vecchio di stale_days giorni.
    Ritorna lista di (vm_name, image_name, last_backup_str, path).
    """
    threshold = datetime.now() - timedelta(days=stale_days)
    stale     = []

    for vm_dir in sorted(SCRIPT_DIR.iterdir()):
        if not vm_dir.is_dir() or vm_dir.name in SKIP_DIRS:
            continue
        for image_dir in sorted(vm_dir.iterdir()):
            if not image_dir.is_dir() or image_dir.name in SKIP_DIRS:
                continue
            last = latest_mtime(image_dir)
            if last is None:
                continue
            if last < threshold:
                age_days = (datetime.now() - last).days
                print(f"  STALE {vm_dir.name}/{image_dir.name}: "
                      f"ultimo backup {last:%Y-%m-%d} ({age_days} giorni fa)")
                stale.append((
                    vm_dir.name,
                    image_dir.name,
                    f"{last:%Y-%m-%d} ({age_days}gg fa)",
                    str(image_dir),
                ))
    return stale


def cleanup_vmdef(vm_dir: Path) -> list:
    """
    Remove VMDEF/<indir> entries whose backupset no longer exists in any image.
    Returns list of (path, result) tuples.
    """
    cleaned = []
    vmdef_dir = vm_dir / "VMDEF"
    if not vmdef_dir.is_dir():
        return cleaned

    # Collect all indir values still present across all images
    remaining_indirs = set()
    for image_dir in vm_dir.iterdir():
        if not image_dir.is_dir() or image_dir.name in SKIP_DIRS:
            continue
        for bkset in image_dir.iterdir():
            if bkset.is_dir() and bkset.name.isdigit() and len(bkset.name) == 6:
                remaining_indirs.add(bkset.name)

    for vmdef_indir in sorted(vmdef_dir.iterdir()):
        if not vmdef_indir.is_dir() or not vmdef_indir.name.isdigit():
            continue
        if vmdef_indir.name not in remaining_indirs:
            print(f"    DELETING VMDEF/{vmdef_indir.name}")
            chattr_remove(vmdef_indir)
            try:
                shutil.rmtree(vmdef_indir)
                result = "SUCCESS"
            except Exception as e:
                print(f"    ERROR: {e}")
                result = "FAIL"
            cleaned.append((str(vmdef_indir), result))

    return cleaned


def apply_retention(retention: int) -> list:
    """
    Scansiona SCRIPT_DIR cercando la struttura vm/image/000001, 000002, ...
    Elimina i backupset più vecchi tenendo solo gli ultimi `retention`.
    Rimuove anche le entry VMDEF orfane per ogni VM.
    Ritorna lista di (path, result).
    """
    purged = []

    for vm_dir in sorted(SCRIPT_DIR.iterdir()):
        if not vm_dir.is_dir() or vm_dir.name in SKIP_DIRS:
            continue
        print(f"CHECKING VM {vm_dir.name}")

        for image_dir in sorted(vm_dir.iterdir()):
            if not image_dir.is_dir() or image_dir.name in SKIP_DIRS:
                continue
            print(f"    Checking IMAGE {image_dir.name}")

            # Backupset = sottocartelle numeriche a 6 cifre (000001, 000002, ...)
            bksets = sorted(
                d for d in image_dir.iterdir()
                if d.is_dir() and d.name.isdigit() and len(d.name) == 6
            )
            if not bksets:
                continue

            max_bkset  = int(bksets[-1].name)
            to_purge_up_to = max_bkset - retention

            print(f"    max backupset={max_bkset:06d}  "
                  f"purging backupsets <= {to_purge_up_to:06d}  "
                  f"(retention={retention})")

            for bkset in bksets:
                if int(bkset.name) <= to_purge_up_to:
                    print(f"    DELETING {bkset}")
                    result = purge_backupset(bkset)
                    purged.append((str(bkset), result))

        purged.extend(cleanup_vmdef(vm_dir))

    return purged


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    print(f"Config: {SCRIPT_DIR / 'config.ini'}")
    print(f"Retention: {cfg['retention']}  Job: {cfg['jobname']}")
    print(f"Email from: {cfg['email_from']}  To: {cfg['rcpt_to']}")
    print("=" * 60)

    purged = apply_retention(cfg["retention"])

    print("\nChecking stale images (> 90 days)...")
    stale  = find_stale_images(stale_days=90)

    if purged:
        print("\nPurged:")
        for path, result in purged:
            print(f"  [{result}] {path}")

    if stale:
        print(f"\nSTALE ALERT: {len(stale)} immagine/i senza backup da oltre 3 mesi")

    if not purged and not stale:
        print("NOTHING to purge, no stale images.")
        return

    html = build_report_html(cfg["jobname"], purged, stale)
    subj = f"CEPH Backup retention — {cfg['jobname']}"
    if stale:
        subj += f" ⚠ {len(stale)} immagine/i stale"
    send_email(cfg, subj, html)


if __name__ == "__main__":
    main()
