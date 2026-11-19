#!/usr/bin/env python3
"""
bksiem.py — log locale per il SIEM (una riga per evento, letta da Fluent Bit).

  /var/log/cephbackup/siem.log   (disco locale: mai su NFS o CephFS)

Formato:
  <ISO 8601 con fuso> cephbackup event=<evento> job=<job> chiave=valore ...

- valori con spazi, virgolette o '=' tra virgolette doppie (quelle interne → ')
- valori tagliati a 500 caratteri, nessun a capo
- nessun segreto (passphrase, contenuto dei file chiave)
- la scrittura non deve mai far fallire né rallentare il backup: ogni errore
  viene ignorato, una sola write() in append per riga

Prova:  ./bksiem.py --test   (scrive event=test job=TEST)

© 2025 — GPLv3
"""

import getpass
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

SIEM_LOG  = Path(os.environ.get("CEPHBACKUP_SIEM_LOG", "/var/log/cephbackup/siem.log"))
MAX_VALUE = 500
_lock     = threading.Lock()


def iso(value=None) -> str:
    """Ora con fuso. value: datetime, stringa 'YYYY-mm-dd HH:MM:SS' (ora locale) o None = adesso."""
    if value is None:
        dt = datetime.now()
    elif isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value)
    return dt.astimezone().isoformat(timespec="seconds")


def current_user() -> str:
    """Chi ha lanciato il comando: SUDO_USER, poi utente di login, poi utente effettivo."""
    user = os.environ.get("SUDO_USER")
    if user:
        return user
    try:
        return os.getlogin()
    except OSError:
        return getpass.getuser()


def _fmt(value) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        value = int(value)
    s = " ".join(str(value).split())           # niente a capo né tab
    if len(s) > MAX_VALUE:
        s = s[:MAX_VALUE - 3] + "..."
    if s == "" or any(c in s for c in ' "='):
        return '"' + s.replace('"', "'") + '"'
    return s


def format_line(event: str, job, **fields) -> str:
    parts = [iso(), "cephbackup", f"event={_fmt(event)}", f"job={_fmt(job)}"]
    parts += [f"{k}={_fmt(v)}" for k, v in fields.items()]
    return " ".join(parts)


def emit(event: str, job, **fields):
    """Scrive una riga nel log SIEM. Non solleva mai eccezioni."""
    try:
        line = (format_line(event, job, **fields) + "\n").encode("utf-8", errors="replace")
        with _lock:
            if not SIEM_LOG.parent.is_dir():
                SIEM_LOG.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(SIEM_LOG.parent, 0o750)
            fd = os.open(SIEM_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except Exception:
        pass


def main():
    if "--test" in sys.argv:
        emit("test", "TEST", host=os.uname().nodename, user=current_user())
        print(f"written: {SIEM_LOG}")
        try:
            print(SIEM_LOG.read_text().splitlines()[-1])
        except OSError as e:
            print(f"cannot read back: {e}")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
