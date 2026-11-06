#!/usr/bin/env python3
"""
bkwatchdog.py — avvisa via email se un job abilitato non completa da troppo tempo.

Copre i casi in cui bkexec.py si blocca senza poter inviare nulla
(es. mount NFS appeso a metà backup). Legge solo backupjobs.json in locale:
non accede mai al target, quindi non può restare bloccato a sua volta.

Soglia: "alert_hours" nel job (default 26). Da mettere in cron una volta al
giorno, dopo la finestra di backup (es. 0 8 * * *).

© 2025 — GPLv3
"""

import subprocess
import sys
from datetime import datetime

from bkexec import load_jobs, send_email

DEFAULT_ALERT_HOURS = 26
TS_FMT = "%Y-%m-%d %H:%M:%S"


def parse_ts(value):
    try:
        return datetime.strptime(value, TS_FMT) if value else None
    except ValueError:
        return None


def running_bkexec():
    """Lista di (pid, ore di esecuzione) dei processi bkexec.py attivi."""
    r = subprocess.run(["ps", "-eo", "pid=,etimes=,args="],
                       capture_output=True, text=True)
    procs = []
    for line in r.stdout.splitlines():
        pid, etimes, args = line.split(None, 2)
        if "bkexec.py" in args and "bkwatchdog" not in args:
            procs.append((int(pid), int(etimes) / 3600))
    return procs


def check_job(job, now):
    """Restituisce il messaggio di allarme per il job, o None se è a posto."""
    limit      = job.get("alert_hours", DEFAULT_ALERT_HOURS)
    last_run   = parse_ts(job.get("lastrun"))
    last_compl = parse_ts(job.get("lastcompletion"))

    if last_run is None and last_compl is None:
        return None     # job nuovo, mai avviato

    if last_compl is None:
        return f"mai completato (avviato il {job.get('lastrun')})"

    age = (now - last_compl).total_seconds() / 3600
    if age <= limit:
        return None

    msg = f"ultimo completamento {job.get('lastcompletion')} ({age:.0f} ore fa, soglia {limit} h)"
    if last_run and last_run > last_compl:
        msg += f"; avviato il {job.get('lastrun')} e mai terminato (lock attivo)"
    return msg


def main():
    now   = datetime.now()
    procs = running_bkexec()
    proc_info = ", ".join(f"PID {p} da {h:.1f} h" for p, h in procs) or "nessuno"
    alerts = 0

    for job in load_jobs():
        if not job.get("enabled", 1):
            continue
        problem = check_job(job, now)
        if not problem:
            print(f"[{job['name']}] OK (ultimo completamento "
                  f"{job.get('lastcompletion') or 'nessuno, mai avviato'})")
            continue

        alerts += 1
        print(f"[{job['name']}] ALLARME: {problem}")
        html = (f"<h3>Job {job['name']}: {problem}</h3>"
                f"<p>Processi bkexec.py in esecuzione: {proc_info}</p>"
                f"<p>Verificare mount del target, log in "
                f"{job['path']}/{job['name']}/LOGS e lock in backupjobs.json.</p>")
        send_email(job, f"[ALERT] CEPH Backup {job['name']} non completato", html)

    print(f"Processi bkexec.py in esecuzione: {proc_info}")
    sys.exit(1 if alerts else 0)


if __name__ == "__main__":
    main()
