#!/usr/bin/env python3
"""
bkrest.py — ripristina un'immagine disco da un restore point.
Versione databaseless di bkrest.php.

USAGE: ./bkrest.py <JOB> <IMAGE> <RESTPOINT> <RESTORED-NAME> [--state]
       ./bkrest.py <JOB> <VM> <RESTPOINT> --state-only

  JOB          nome del job (da backupjobs.json)
  IMAGE        nome dell'immagine CEPH (es. PANTHERA-sda)
  RESTPOINT    punto di ripristino nel formato 000001-000003
  RESTORED-NAME nome della nuova immagine CEPH da creare
  --state      ripristina anche NVRAM UEFI e stato TPM del restore point sulla VM
               originale (percorsi letti dal suo XML in vm_config_path).
               La VM deve essere spenta; i file attuali sono rinominati .bak-<data>
  --state-only solo NVRAM/TPM, senza dischi (secondo argomento = nome della VM)

Esempio:
  ./bkrest.py NAS11day_01 PANTHERA-sda 000001-000003 PANTHERA-sda_rest
  ./bkrest.py JOBNAME MYVM 000002-000004 --state-only

© 2025 — GPLv3
"""

import configparser
import getpass
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import bkcrypt
import bksiem
import bkstate

SCRIPT_DIR = Path(__file__).parent
RESTORE_LOG_DIR = SCRIPT_DIR / "RESTORELOGS"
STARTED  = datetime.now()
LOG_FILE = None
SIEM     = {}       # campi di restore_start, per restore_end
LAST_ERROR = ""


def lg(msg=""):
    """Stampa a video e scrive su RESTORELOGS con data/ora."""
    global LAST_ERROR
    print(msg)
    if str(msg).startswith("ERROR"):
        LAST_ERROR = str(msg)
    if LOG_FILE is None:
        return
    try:
        RESTORE_LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    return cfg


def load_jobs():
    with open(SCRIPT_DIR / "backupjobs.json") as f:
        return json.load(f)


def find_job(jobs, job_name):
    for job in jobs:
        if job["name"] == job_name:
            return job
    return None


def find_vm_for_image(job_dir, image_name):
    """
    Cerca in job_dir/$vm/$image_name la VM che contiene l'immagine.
    Restituisce il nome della VM o None.
    """
    SKIP = {"LOGS"}
    for vm_dir in job_dir.iterdir():
        if not vm_dir.is_dir() or vm_dir.name in SKIP:
            continue
        if (vm_dir / image_name).is_dir():
            return vm_dir.name
    return None


def run(cmd):
    """Esegue un comando mostrando output live. Restituisce il return code."""
    lg(" ".join(cmd))
    result = subprocess.run(cmd)
    lg(f"  rc={result.returncode}")
    return result.returncode


def resolve_key(job, enc_files, tpm_file=None):
    """
    Chiave per i file cifrati: quella registrata nel job se valida,
    altrimenti richiesta all'utente (3 tentativi). Restituisce (key_file, passphrase).
    tpm_file: verifica su uno stato TPM cifrato invece che su un diff rbd.
    """
    def check(key_file=None, passphrase=None):
        if not tpm_file:
            return bkcrypt.check_key(enc_files[0], key_file=key_file, passphrase=passphrase)
        try:
            bkcrypt.decrypt_bytes(tpm_file, key_file, passphrase)
            return True, "key OK"
        except OSError as e:
            return False, str(e)

    key_file = bkcrypt.key_file_for(job)
    if key_file:
        ok, msg = check(key_file=key_file)
        if ok:
            lg(f"Encryption key: file registrato nel job ({key_file}) — valida")
            return key_file, None
        lg(f"WARNING: la chiave registrata ({key_file}) non apre il backup: {msg}")
    else:
        lg("Encryption key: nessuna chiave registrata per il job")

    if not sys.stdin.isatty():
        lg("ERROR: backup cifrato e nessun terminale per chiedere la chiave")
        sys.exit(1)
    for attempt in range(1, 4):
        passphrase = getpass.getpass(f"Chiave di cifratura per il job {job['name']}: ")
        ok, msg = check(passphrase=passphrase)
        if ok:
            lg("Encryption key: inserita manualmente — valida")
            return None, passphrase
        lg(f"Chiave non valida (tentativo {attempt}/3): {msg}")
    lg("ERROR: chiave non valida, restore annullato")
    sys.exit(1)


def siem_start(job_name, vm, image, restpoint, dest):
    SIEM.update(job=job_name, vm=vm, image=image, restpoint=restpoint, dest=dest,
                user=bksiem.current_user())
    bksiem.emit("restore_start", job_name, **{k: v for k, v in SIEM.items() if k != "job"})


def siem_end(result, error=""):
    if not SIEM or SIEM.get("ended"):
        return
    SIEM["ended"] = True
    fields = {k: v for k, v in SIEM.items() if k not in ("job", "ended")}
    fields["result"] = result
    if error:
        fields["error"] = error
    bksiem.emit("restore_end", SIEM["job"], **fields)


def restore_state(cfg, job, job_dir, vm_name, instance, incset, key_file, passphrase):
    """NVRAM/TPM del restore point sulla VM originale (spenta)."""
    vm_cfg   = Path(cfg["libvirt"]["vm_config_path"])
    xml_path = vm_cfg / f"{vm_name}.xml"
    lg()
    lg(f"NVRAM / TPM della VM {vm_name} (restore point {instance}-{incset})")
    if not xml_path.exists():
        lg(f"ERROR: XML della VM non trovato: {xml_path}")
        sys.exit(1)
    nvram_dst, tpm_dst = bkstate.state_paths(xml_path.read_text())
    if not nvram_dst and not tpm_dst:
        lg("La VM non usa NVRAM UEFI né TPM: niente da ripristinare")
        return
    running, where = bkstate.vm_running(vm_name)
    if running is not False:
        lg(f"ERROR: la VM deve essere spenta ({where})")
        sys.exit(1)
    lg(f"VM spenta: {where}")
    st = bkstate.point_state(job_dir / vm_name / "VMDEF" / instance / incset, vm_name)
    if st["tpm_encrypted"] and not (key_file or passphrase):
        key_file, passphrase = resolve_key(job, [], tpm_file=st["tpm"][0])
    try:
        bkstate.install_state(st, nvram_dst, tpm_dst, key_file, passphrase, lg=lg)
    except OSError as e:
        lg(f"ERROR: ripristino NVRAM/TPM fallito: {e}")
        sys.exit(1)


def main():
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    state_only = "--state-only" in flags
    with_state = "--state" in flags or state_only
    if len(args) < (3 if state_only else 4):
        print(__doc__)
        sys.exit(1)

    job_name      = args[0]
    vm_image      = args[1]
    restore_point = args[2]
    restored_name = f"{vm_image}-state" if state_only else args[3]

    global LOG_FILE
    LOG_FILE = RESTORE_LOG_DIR / f"{STARTED:%Y%m%d_%H%M%S}_{restored_name}.log"

    # Valida formato RESTPOINT
    parts = restore_point.split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        lg(f"ERROR: RESTPOINT deve essere nel formato 000001-000003")
        sys.exit(1)

    instance = parts[0]   # es. 000001 (set full)
    point    = int(parts[1])  # es. 3 (fino all'inc 000003)

    cfg  = load_config()
    pool = cfg["ceph"]["poolname"]
    jobs = load_jobs()

    job = find_job(jobs, job_name)
    if not job:
        lg(f"ERROR: job '{job_name}' non trovato in backupjobs.json")
        sys.exit(1)

    job_dir = Path(job["path"]) / job_name

    if state_only:
        if not (job_dir / vm_image).is_dir():
            lg(f"ERROR: VM '{vm_image}' non trovata nel job '{job_name}'")
            sys.exit(1)
        siem_start(job_name, vm_image, "-", restore_point, "state")
        restore_state(cfg, job, job_dir, vm_image, instance, parts[1], None, None)
        siem_end("OK")
        lg(f"Log: {LOG_FILE}")
        return

    # Trova la VM che contiene l'immagine
    vm_name = find_vm_for_image(job_dir, vm_image)
    if not vm_name:
        lg(f"ERROR: immagine '{vm_image}' non trovata nel job '{job_name}'")
        sys.exit(1)

    # Da qui in poi ogni uscita con errore produce restore_end result=ERROR
    siem_start(job_name, vm_name, vm_image, restore_point,
               f"{pool}/{restored_name}" + ("+state" if with_state else ""))

    backup_path = job_dir / vm_name / vm_image / instance
    if not backup_path.is_dir():
        lg(f"ERROR: cartella backup non trovata: {backup_path}")
        sys.exit(1)

    lg(f"Job:          {job_name}")
    lg(f"VM:           {vm_name}")
    lg(f"Image:        {vm_image}")
    lg(f"Restore point: {restore_point}")
    lg(f"Backup path:  {backup_path}")
    lg(f"Destination:  {pool}/{restored_name}")
    lg()

    # Verifica che tutti i diff esistano prima di creare l'immagine
    diff_files = [bkcrypt.backup_file(backup_path, str(x).zfill(6)) for x in range(point + 1)]
    for diff_file in diff_files:
        if not diff_file.exists():
            lg(f"ERROR: file diff non trovato: {diff_file}")
            sys.exit(1)

    enc_files = [f for f in diff_files if bkcrypt.is_encrypted(f)]
    key_file = passphrase = None
    if enc_files:
        lg(f"Backup cifrato: {len(enc_files)}/{len(diff_files)} file")
        key_file, passphrase = resolve_key(job, enc_files)
        lg()

    if with_state:
        # controllo prima di creare l'immagine: con la VM accesa NVRAM/TPM non si toccano
        running, where = bkstate.vm_running(vm_name)
        if running is not False:
            lg(f"ERROR: --state richiede la VM {vm_name} spenta ({where})")
            sys.exit(1)

    # Crea immagine RBD vuota di destinazione
    lg(f"Creating empty image {restored_name} ...")
    rc = run(["rbd", "create", restored_name, "--size", "1024", "-p", pool])
    if rc != 0:
        lg(f"ERROR: rbd create fallito (rc={rc})")
        sys.exit(1)

    # Applica i diff dal full (000000) fino al punto richiesto
    for diff_file in diff_files:
        lg()
        lg(f"Importing diff {diff_file} ...")
        if diff_file in enc_files:
            target = f"{pool}/{restored_name}"
            lg(f"gpg --decrypt {diff_file} | rbd import-diff - {target}")
            rc, errlines = bkcrypt.import_encrypted(diff_file, target, key_file, passphrase)
            for line in errlines:
                lg(f"  {line}")
            lg(f"  rc={rc}")
        else:
            rc = run(["rbd", "import-diff", str(diff_file), f"{pool}/{restored_name}"])
        if rc != 0:
            lg(f"ERROR: rbd import-diff fallito su {diff_file} (rc={rc})")
            sys.exit(1)

    if with_state:
        restore_state(cfg, job, job_dir, vm_name, instance, parts[1], key_file, passphrase)
    else:
        vmdef = job_dir / vm_name / "VMDEF" / instance / parts[1]
        xml = vmdef / f"{vm_name}.xml"
        try:
            uses_nvram = xml.exists() and bkstate.state_paths(xml.read_text())[0]
        except Exception:
            uses_nvram = False
        if uses_nvram:
            lg()
            lg("NOTA: la VM usa NVRAM UEFI e qui è stato ripristinato solo il disco. Se Windows ha")
            lg("aggiornato la lista di revoca Secure Boot (DBX) dopo questo restore point, il boot")
            lg("manager vecchio potrebbe non avviarsi con l'NVRAM attuale: ripristinare anche")
            lg("l'NVRAM di questo punto (--state / --state-only) oppure rinominare l'NVRAM")
            lg("attuale perché libvirt lo ricrei dal template.")

    siem_end("OK")
    elapsed = int((datetime.now() - STARTED).total_seconds())
    lg()
    lg(f"Restore completato: {pool}/{restored_name}")
    lg(f"Tempo totale: {elapsed // 60}m {elapsed % 60:02d}s")
    lg(f"Log: {LOG_FILE}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            siem_end("ERROR", LAST_ERROR or f"exit code {e.code}")
        raise
    except BaseException as e:
        siem_end("ERROR", f"{type(e).__name__}: {e}")
        raise
