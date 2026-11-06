"""
bkcrypt.py — cifratura opzionale dei backup con gpg (AES256, passphrase).

I file cifrati hanno il suffisso .gpg (000003.gpg); una catena può essere
mista (file vecchi in chiaro, nuovi cifrati). Come ulteriore controllo un file
in chiaro inizia sempre con l'header di rbd ("rbd diff v"). La passphrase non
passa mai sulla riga di comando: da file (--passphrase-file) o da pipe
(--passphrase-fd).

© 2025 — GPLv3
"""

import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

RBD_DIFF_MAGIC = b"rbd diff v"
ENC_SUFFIX     = ".gpg"      # 000003 = in chiaro, 000003.gpg = cifrato
DEFAULT_KEY_DIR = Path("/root/.cephbackup/keys")
MIN_KEY_LEN    = 12

GPG_BASE = ["gpg", "--batch", "--quiet", "--yes", "--no-tty",
            "--pinentry-mode", "loopback"]


def key_file_for(job: dict):
    """Path del file chiave registrato nel job, se esiste; altrimenti None."""
    kf = job.get("encryption_key_file")
    return Path(kf) if kf and Path(kf).is_file() else None


def default_key_path(job: dict) -> Path:
    return Path(job.get("encryption_key_file") or DEFAULT_KEY_DIR / f"{job['name']}.key")


def generate_key() -> str:
    return secrets.token_urlsafe(36)


def write_key_file(path, key: str):
    """Scrive la chiave con permessi 600 (cartella 700), senza finestra in cui sia leggibile."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, key.encode() + b"\n")
    finally:
        os.close(fd)


def latest_files_per_image(job_dir) -> list:
    """Ultimo file di backup del set più recente, per ogni VM/immagine del job."""
    out = []
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return out
    for vm_dir in sorted(d for d in job_dir.iterdir() if d.is_dir() and d.name != "LOGS"):
        for image_dir in sorted(d for d in vm_dir.iterdir() if d.is_dir() and d.name != "VMDEF"):
            sets = sorted(d for d in image_dir.iterdir() if d.is_dir() and d.name.isdigit())
            if not sets:
                continue
            files = sorted((f for f in sets[-1].iterdir()
                            if f.is_file() and backup_index(f.name) and f.stat().st_size > 0),
                           key=lambda f: f.name)
            if files:
                out.append(files[-1])
    return out


def latest_encrypted_file(job_dir):
    """Il file cifrato più recente del job (per verificare una chiave), o None."""
    # job/VM/immagine/set/NNNNNN.gpg — basta il nome, nessuna lettura dei file
    enc = [f for f in Path(job_dir).glob(f"*/*/*/*{ENC_SUFFIX}")
           if backup_index(f.name) and f.stat().st_size > 0]
    return max(enc, key=lambda f: f.stat().st_mtime) if enc else None


def backup_index(name: str):
    """'000003' o '000003.gpg' -> '000003'; altri nomi -> None."""
    base = name[:-len(ENC_SUFFIX)] if name.endswith(ENC_SUFFIX) else name
    return base if base.isdigit() and len(base) == 6 else None


def backup_file(set_dir, index: str) -> Path:
    """File di backup per l'indice dato, cifrato o in chiaro (catene miste)."""
    enc = Path(set_dir) / (index + ENC_SUFFIX)
    return enc if enc.exists() else Path(set_dir) / index


def is_encrypted(path) -> bool:
    if Path(path).name.endswith(ENC_SUFFIX):
        return True
    with open(path, "rb") as f:
        head = f.read(len(RBD_DIFF_MAGIC))
    return bool(head) and not head.startswith(RBD_DIFF_MAGIC)


def _passphrase_args(key_file=None, passphrase=None):
    """Restituisce (argomenti gpg, pass_fds, fd da chiudere dopo il Popen)."""
    if key_file:
        return ["--passphrase-file", str(key_file)], (), None
    r, w = os.pipe()
    os.write(w, passphrase.encode() + b"\n")
    os.close(w)
    return ["--passphrase-fd", str(r)], (r,), r


def run_encrypted_export(rbd_cmd, out_path, key_file, timeout):
    """
    rbd export-diff ... - | gpg --symmetric -o out_path
    rbd_cmd deve scrivere su stdout ("-" come destinazione).
    Restituisce CompletedProcess (returncode, stderr); solleva TimeoutExpired.
    """
    gpg_cmd = GPG_BASE + ["--passphrase-file", str(key_file), "--symmetric",
                          "--cipher-algo", "AES256", "--compress-algo", "none",
                          "-o", str(out_path)]
    # stderr di rbd su file temporaneo: le righe di avanzamento non devono riempire una pipe
    with tempfile.TemporaryFile() as rbd_err:
        rbd = subprocess.Popen(rbd_cmd, stdout=subprocess.PIPE, stderr=rbd_err)
        gpg = subprocess.Popen(gpg_cmd, stdin=rbd.stdout, stderr=subprocess.PIPE)
        rbd.stdout.close()
        deadline = time.monotonic() + timeout
        try:
            _, gpg_err = gpg.communicate(timeout=timeout)
            rbd.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            for p in (rbd, gpg):
                p.kill()
            for p in (rbd, gpg):
                p.wait()
            raise subprocess.TimeoutExpired(rbd_cmd, timeout)
        rbd_err.seek(0)
        err = rbd_err.read().decode(errors="replace") + gpg_err.decode(errors="replace")
    return subprocess.CompletedProcess(rbd_cmd, rbd.returncode or gpg.returncode, None, err)


def decrypt_popen(path, stdout, key_file=None, passphrase=None):
    """Avvia gpg --decrypt su path; il chiamante legge da .stdout."""
    args, fds, rfd = _passphrase_args(key_file, passphrase)
    proc = subprocess.Popen(GPG_BASE + args + ["--decrypt", str(path)],
                            stdout=stdout, stderr=subprocess.PIPE, pass_fds=fds)
    if rfd is not None:
        os.close(rfd)
    return proc


def _last_line(data: bytes, default: str) -> str:
    lines = [l for l in data.decode(errors="replace").splitlines() if l.strip()]
    return lines[-1] if lines else default


def check_key(path, key_file=None, passphrase=None):
    """
    Verifica rapida: decifra l'inizio del file e controlla l'header rbd.
    Restituisce (ok, messaggio).
    """
    proc = decrypt_popen(path, subprocess.PIPE, key_file, passphrase)
    head = proc.stdout.read(len(RBD_DIFF_MAGIC))
    proc.stdout.close()
    if proc.poll() is None:
        proc.kill()
    err = proc.stderr.read()
    proc.wait()
    if head.startswith(RBD_DIFF_MAGIC):
        return True, "key OK"
    return False, _last_line(err, "decrypted data is not an rbd diff")


def verify_full(path, key_file=None, passphrase=None):
    """Decifra l'intero file (controllo integrità MDC di gpg). Restituisce (ok, messaggio)."""
    proc = decrypt_popen(path, subprocess.PIPE, key_file, passphrase)
    head = proc.stdout.read(len(RBD_DIFF_MAGIC))
    while proc.stdout.read(1 << 20):
        pass
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode == 0 and head.startswith(RBD_DIFF_MAGIC):
        return True, "key OK, integrity OK"
    return False, _last_line(err, f"gpg exit code {proc.returncode}")


def import_encrypted(path, rbd_target, key_file=None, passphrase=None, runner=None):
    """
    gpg --decrypt path | rbd import-diff - rbd_target
    runner(cmd, stdin) -> (returncode, errlines) permette alla UI di mostrare
    l'avanzamento; deve chiudere stdin dopo aver avviato il processo.
    Restituisce (returncode, errlines).
    """
    gpg = decrypt_popen(path, subprocess.PIPE, key_file, passphrase)
    cmd = ["rbd", "import-diff", "-", rbd_target]
    if runner:
        rc, errlines = runner(cmd, gpg.stdout)
    else:
        rbd = subprocess.Popen(cmd, stdin=gpg.stdout)
        gpg.stdout.close()
        rc, errlines = rbd.wait(), []
    gpg_err = gpg.stderr.read()
    gpg.wait()
    if gpg.returncode != 0:
        errlines = errlines + [_last_line(gpg_err, f"gpg exit code {gpg.returncode}")]
    return rc or gpg.returncode, errlines
