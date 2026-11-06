#!/usr/bin/env python3
"""
bkcheckkey.py — verifica che una chiave apra i backup cifrati di un job.

USAGE: ./bkcheckkey.py JOB [VM] [--full] [--ask]

  JOB     nome del job (da backupjobs.json)
  VM      limita la verifica a una VM
  --full  decifra per intero tutti i file del set più recente (verifica anche
          l'integrità; lento). Senza: solo l'inizio dell'ultimo file per immagine
  --ask   chiede la chiave anche se il job ne ha una registrata
          (es. per verificare la copia conservata fuori dal cluster)

Exit code: 0 tutto OK, 1 almeno un file non si apre, 2 errore di utilizzo.

© 2025 — GPLv3
"""

import getpass
import sys
from pathlib import Path

import bkcrypt
from bkexec import is_mounted, load_jobs


def latest_set_files(image_dir: Path) -> list:
    sets = sorted(d for d in image_dir.iterdir() if d.is_dir() and d.name.isdigit())
    if not sets:
        return []
    return sorted((f for f in sets[-1].iterdir() if f.is_file() and bkcrypt.backup_index(f.name)),
                  key=lambda f: f.name)


def main():
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    full  = "--full" in sys.argv
    ask   = "--ask" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(2)

    job_name = args[0]
    only_vm  = args[1] if len(args) > 1 else None
    job = next((j for j in load_jobs() if j["name"] == job_name), None)
    if not job:
        print(f"ERROR: job '{job_name}' non trovato in backupjobs.json")
        sys.exit(2)

    if job.get("checkmount") and not is_mounted(job.get("mountpoint", "")):
        print(f"ERROR: mountpoint {job.get('mountpoint')} non montato o non risponde")
        sys.exit(2)

    job_dir = Path(job["path"]) / job_name
    key_file, passphrase = bkcrypt.key_file_for(job), None
    if ask or key_file is None:
        passphrase = getpass.getpass(f"Chiave di cifratura per il job {job_name}: ")
        key_file = None
        print("Chiave: inserita manualmente")
    else:
        print(f"Chiave: file registrato nel job ({key_file})")
    print(f"Modalità: {'completa (decifra tutto il set più recente)' if full else 'rapida (header dell ultimo file)'}")
    print()

    ok_n = bad_n = plain_n = 0
    for vm_dir in sorted(d for d in job_dir.iterdir() if d.is_dir() and d.name != "LOGS"):
        if only_vm and vm_dir.name != only_vm:
            continue
        for image_dir in sorted(d for d in vm_dir.iterdir() if d.is_dir() and d.name != "VMDEF"):
            files = latest_set_files(image_dir)
            if not full:
                files = files[-1:]
            for f in files:
                rel = f.relative_to(job_dir)
                if f.stat().st_size == 0:
                    continue
                if not bkcrypt.is_encrypted(f):
                    plain_n += 1
                    print(f"  IN CHIARO   {rel}")
                    continue
                check = bkcrypt.verify_full if full else bkcrypt.check_key
                ok, msg = check(f, key_file=key_file, passphrase=passphrase)
                if ok:
                    ok_n += 1
                    print(f"  OK          {rel}")
                else:
                    bad_n += 1
                    print(f"  NON VALIDA  {rel}  ({msg})")

    print()
    print(f"Riepilogo: {ok_n} OK, {bad_n} non validi, {plain_n} in chiaro")
    if ok_n + bad_n == 0:
        print("Nessun file cifrato trovato.")
    sys.exit(1 if bad_n else 0)


if __name__ == "__main__":
    main()
