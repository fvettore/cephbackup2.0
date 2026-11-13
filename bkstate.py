#!/usr/bin/env python3
"""
bkstate.py — backup e restore dello stato della VM fuori dai dischi:
NVRAM UEFI (<nvram>) e stato del TPM virtuale (<tpm><backend type='emulator'>).

I percorsi si leggono sempre dall'XML della VM, mai ricostruiti dal nome.
Nel backup i file finiscono accanto all'XML:

  VMDEF/<indir>/<incset>/<VM>_VARS.fd              NVRAM (in chiaro)
  VMDEF/<indir>/<incset>/TPM/tpm2-00.permall[.gpg] stato TPM (cifrato se il job è cifrato)

© 2025 — GPLv3
"""

import os
import pwd
import grp
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import bkcrypt

TPM_SUBDIR   = "TPM"
TPM_SKIP     = {".lock"}          # lock di swtpm: non si copia
NVRAM_OWNER  = ("qemu", "qemu")
TPM_OWNER    = ("tss", "tss")


# ─────────────────────────────────────────────────────── config ──
#
# config.ini (tutto facoltativo):
#
#   [vmstate]
#   backup            = 1     (0 = non salvare NVRAM/TPM)
#   nvram_restore_dir =       (cartella per l'NVRAM delle VM ripristinate; vuoto = come l'originale)
#   tpm_restore_dir   =       (cartella che contiene le cartelle TPM delle VM ripristinate; idem)
#
#   I commenti vanno su righe proprie: configparser non toglie quelli in linea.

def load_state_config(cfg) -> dict:
    """cfg: configparser già letto."""
    sec = "vmstate"
    try:
        backup = cfg.getboolean(sec, "backup", fallback=True)
    except ValueError:
        backup = True       # valore non valido: meglio salvare che perdere lo stato
    return {
        "backup":            backup,
        "nvram_restore_dir": cfg.get(sec, "nvram_restore_dir", fallback="").strip(),
        "tpm_restore_dir":   cfg.get(sec, "tpm_restore_dir",   fallback="").strip(),
    }


def restore_targets(orig_nvram, orig_tpm, new_vm_name: str, scfg: dict):
    """
    Percorsi per NVRAM e TPM di una VM ripristinata con nome new_vm_name:
    <dir>/<new_vm_name>_VARS.fd e <dir>/<new_vm_name>, dove <dir> è quella di
    config.ini oppure la cartella dell'originale. None se la VM non li usa.
    """
    nvram_dst = tpm_dst = None
    if orig_nvram:
        base = Path(scfg.get("nvram_restore_dir") or Path(orig_nvram).parent)
        nvram_dst = base / f"{new_vm_name}_VARS.fd"
    if orig_tpm:
        base = Path(scfg.get("tpm_restore_dir") or Path(orig_tpm).parent)
        tpm_dst = base / new_vm_name
    return nvram_dst, tpm_dst


# ───────────────────────────────────────────────────────── XML ──

def state_paths(xml_text: str):
    """(percorso NVRAM o None, cartella TPM o None) dall'XML della VM."""
    root = ET.fromstring(xml_text)
    nvram = root.find("./os/nvram")
    nvram_path = (nvram.text or "").strip() if nvram is not None else ""
    tpm_dir = ""
    src = root.find("./devices/tpm/backend[@type='emulator']/source[@type='dir']")
    if src is not None:
        tpm_dir = (src.get("path") or "").strip()
    return (Path(nvram_path) if nvram_path else None,
            Path(tpm_dir) if tpm_dir else None)


def patch_state_paths(xml_text: str, nvram_path=None, tpm_dir=None) -> str:
    """Sostituisce nell'XML il percorso dell'NVRAM e/o della cartella TPM."""
    if nvram_path is not None:
        xml_text = re.sub(r"(<nvram\b[^>]*>)[^<]*(</nvram>)",
                          lambda m: f"{m.group(1)}{nvram_path}{m.group(2)}",
                          xml_text, count=1)
    if tpm_dir is not None:
        def _tpm(m):
            return re.sub(r"(<source\b[^>]*\btype=['\"]dir['\"][^>]*\bpath=)(['\"])[^'\"]*\2",
                          lambda s: f"{s.group(1)}{s.group(2)}{tpm_dir}{s.group(2)}",
                          m.group(0), count=1)
        xml_text = re.sub(r"<tpm\b.*?</tpm>", _tpm, xml_text, count=1, flags=re.DOTALL)
    return xml_text


# ────────────────────────────────────────────────────── backup ──

def stable_read(path: Path, tries: int = 5) -> bytes:
    """Legge un file che può essere scritto a VM accesa: due letture uguali con
    mtime/size invariati, altrimenti riprova. OSError se non si stabilizza."""
    for _ in range(tries):
        st1 = path.stat()
        a = path.read_bytes()
        b = path.read_bytes()
        st2 = path.stat()
        if a == b and (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size):
            return a
        time.sleep(1)
    raise OSError(f"{path} keeps changing while reading")


def _write_file(dst: Path, data: bytes, key_file=None):
    """Scrive dst (via file temporaneo + rename), mode 600; cifrato se key_file."""
    tmp = dst.with_name(f".{dst.name}.tmp")
    if key_file:
        bkcrypt.encrypt_bytes(data, tmp, key_file)
    else:
        tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)


def save_state(xml_text: str, vm_name: str, dest_dir: Path, key_file=None, lg=print) -> list:
    """
    Copia NVRAM e TPM della VM in dest_dir (VMDEF/<indir>/<incset>/).
    NVRAM: in chiaro, riscritto solo se cambiato. TPM: cifrato se key_file
    (riscritto sempre, sono pochi KB), altrimenti riscritto solo se cambiato.
    Non solleva eccezioni: restituisce la lista dei warning.
    """
    warnings = []
    try:
        nvram_src, tpm_src = state_paths(xml_text)
    except ET.ParseError as e:
        return [f"WARNING: NVRAM/TPM not saved, XML parse error: {e}"]

    if nvram_src:
        dst = dest_dir / f"{vm_name}_VARS.fd"
        try:
            data = stable_read(nvram_src)
            if dst.exists() and dst.read_bytes() == data:
                lg(f"NVRAM already up to date at {dst}")
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                _write_file(dst, data)
                lg(f"NVRAM copied to {dst}")
        except OSError as e:
            warnings.append(f"WARNING: cannot save NVRAM {nvram_src}: {e}")

    if tpm_src:
        tdir = dest_dir / TPM_SUBDIR
        try:
            files = sorted(f for f in tpm_src.iterdir()
                           if f.is_file() and f.name not in TPM_SKIP)
            if not files:
                raise OSError(f"no TPM state files in {tpm_src}")
            tdir.mkdir(parents=True, exist_ok=True)
            keep = set()
            for f in files:
                data = stable_read(f)
                dst = tdir / (f.name + (bkcrypt.ENC_SUFFIX if key_file else ""))
                keep.add(dst.name)
                if not key_file and dst.exists() and dst.read_bytes() == data:
                    continue
                _write_file(dst, data, key_file)
            # file di uno stato precedente nello stesso slot (es. versione in chiaro/cifrata)
            for old in tdir.iterdir():
                if old.is_file() and old.name not in keep:
                    old.unlink()
            lg(f"TPM state saved to {tdir} ({len(files)} file{'s' if len(files) > 1 else ''}"
               f"{', encrypted' if key_file else ''})")
        except OSError as e:
            warnings.append(f"WARNING: cannot save TPM state {tpm_src}: {e}")

    for w in warnings:
        lg(w)
    return warnings


# ───────────────────────────────────────────────── restore point ──

def point_state(vmdef_dir: Path, vm_name: str) -> dict:
    """Cosa c'è nel restore point: {'nvram': Path|None, 'tpm': [Path], 'tpm_encrypted': bool}."""
    nvram = vmdef_dir / f"{vm_name}_VARS.fd"
    tdir  = vmdef_dir / TPM_SUBDIR
    tpm   = sorted(f for f in tdir.iterdir() if f.is_file()) if tdir.is_dir() else []
    return {
        "nvram":         nvram if nvram.is_file() else None,
        "tpm":           tpm,
        "tpm_encrypted": any(f.name.endswith(bkcrypt.ENC_SUFFIX) for f in tpm),
    }


def state_flags(vmdef_dir: Path, vm_name: str) -> str:
    """Sigla per gli elenchi: 'N' = NVRAM, 'T' = TPM, '-' = assente."""
    st = point_state(vmdef_dir, vm_name)
    return ("N" if st["nvram"] else "-") + ("T" if st["tpm"] else "-")


def _chown(path: Path, owner):
    try:
        shutil.chown(path, user=pwd.getpwnam(owner[0]).pw_uid, group=grp.getgrnam(owner[1]).gr_gid)
    except (KeyError, OSError):
        pass        # utente assente (es. macchina senza swtpm): restano i permessi


def _backup_existing(path: Path, lg):
    if path.exists():
        bak = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d_%H%M%S}")
        path.rename(bak)
        lg(f"  existing {path} renamed to {bak.name}")


def install_state(point: dict, nvram_dst=None, tpm_dst=None,
                  key_file=None, passphrase=None, lg=print) -> list:
    """
    Installa lo stato di un restore point (da point_state) nei percorsi indicati.
    Eventuali file/cartelle già presenti vengono rinominati .bak-<data>.
    Se il restore point non ha l'NVRAM: nessun file (libvirt lo ricrea dal template).
    Se non ha il TPM: cartella vuota (swtpm crea un TPM nuovo).
    Restituisce la lista dei warning; solleva OSError sugli errori di decifratura/scrittura.
    """
    warnings = []
    if nvram_dst:
        nvram_dst = Path(nvram_dst)
        _backup_existing(nvram_dst, lg)
        if point["nvram"]:
            nvram_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(point["nvram"], nvram_dst)
            os.chmod(nvram_dst, 0o600)
            _chown(nvram_dst, NVRAM_OWNER)
            lg(f"  NVRAM restored to {nvram_dst}")
        else:
            warnings.append("restore point without NVRAM: libvirt will create a new one "
                            "from the template (boot entries and Secure Boot keys reset)")
    if tpm_dst:
        tpm_dst = Path(tpm_dst)
        _backup_existing(tpm_dst, lg)
        tpm_dst.mkdir(parents=True)
        os.chmod(tpm_dst, 0o700)
        _chown(tpm_dst, TPM_OWNER)
        for f in point["tpm"]:
            if f.name.endswith(bkcrypt.ENC_SUFFIX):
                data = bkcrypt.decrypt_bytes(f, key_file, passphrase)
                name = f.name[:-len(bkcrypt.ENC_SUFFIX)]
            else:
                data, name = f.read_bytes(), f.name
            dst = tpm_dst / name
            dst.write_bytes(data)
            os.chmod(dst, 0o600)
            _chown(dst, TPM_OWNER)
        if point["tpm"]:
            lg(f"  TPM state restored to {tpm_dst}")
        else:
            warnings.append("restore point without TPM state: the VM gets a new empty TPM "
                            "(BitLocker will ask for the recovery key)")
    for w in warnings:
        lg(f"  WARNING: {w}")
    return warnings


# ─────────────────────────────────────────────────── VM accesa? ──

def vm_running(vm_name: str):
    """
    (True/False, dove) — prima Pacemaker (risorsa VM_<nome>, vale per tutto il cluster),
    poi virsh locale. None se non si riesce a stabilirlo.
    """
    try:
        r = subprocess.run(["crm_resource", "--locate", "--resource", f"VM_{vm_name}"],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0 and "is running on" in out:
            return True, out
        if r.returncode == 0 and "NOT running" in out:
            return False, out
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(["virsh", "domstate", vm_name], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            state = r.stdout.strip()
            return state not in ("shut off", "spento"), f"virsh (local): {state}"
        return False, "not defined on this node (virsh)"
    except (OSError, subprocess.TimeoutExpired):
        return None, "cannot determine VM state"
