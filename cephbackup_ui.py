#!/usr/bin/env python3
"""
CephBackup UI
Text-based interface for managing CEPH KVM backups.
Databaseless — reads from config.ini and backupjobs.json.

© 2025 — GPLv3
"""

import configparser
import curses
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TMP_DIR    = SCRIPT_DIR / 'tmp'
SKIP_DIRS  = {"LOGS", "VMDEF"}

# ── Colori ────────────────────────────────────────────────────────────────────

C_TITLE  = 1
C_SELECT = 2
C_STATUS = 3
C_HEADER = 4
C_DIM    = 5
C_OK     = 6
C_ERR    = 7


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE,  curses.COLOR_CYAN,    -1)
    curses.init_pair(C_SELECT, curses.COLOR_BLACK,   curses.COLOR_CYAN)
    curses.init_pair(C_STATUS, curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(C_HEADER, curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_DIM,    curses.COLOR_WHITE,   -1)
    curses.init_pair(C_OK,     curses.COLOR_GREEN,   -1)
    curses.init_pair(C_ERR,    curses.COLOR_RED,     -1)


# ── Config / jobs ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    return {
        'poolname':       cfg.get('ceph',    'poolname',       fallback='rbdpool01'),
        'vm_config_path': cfg.get('libvirt', 'vm_config_path', fallback='/etc/libvirt/qemu'),
    }


def load_jobs() -> list:
    p = SCRIPT_DIR / "backupjobs.json"
    if not p.exists():
        p.write_text("[]")
    with open(p) as f:
        return json.load(f)


def save_jobs(jobs: list):
    tmp = SCRIPT_DIR / "backupjobs.json.tmp"
    tmp.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))
    tmp.replace(SCRIPT_DIR / "backupjobs.json")


# ── Dati da filesystem ────────────────────────────────────────────────────────

def fetch_vms(jobs: list) -> list:
    """Unique VM names, obtained by scanning job directories."""
    seen = set()
    vms  = []
    for job in jobs:
        job_dir = Path(job['path']) / job['name']
        if not job_dir.is_dir():
            continue
        for d in sorted(job_dir.iterdir()):
            if d.is_dir() and d.name not in {'LOGS'} and d.name not in seen:
                seen.add(d.name)
                vms.append(d.name)
    return sorted(vms)


def fetch_restore_points(jobs: list, vm_name: str) -> list:
    """Available restore points for a VM, obtained from the filesystem."""
    points = []
    for job in jobs:
        job_name = job['name']
        job_path = job['path']
        vm_dir   = Path(job_path) / job_name / vm_name
        if not vm_dir.is_dir():
            continue
        images = sorted(
            d for d in vm_dir.iterdir()
            if d.is_dir() and d.name not in SKIP_DIRS
        )
        for image_dir in images:
            full_dirs = sorted(
                d for d in image_dir.iterdir()
                if d.is_dir() and d.name.isdigit() and len(d.name) == 6
            )
            for full_dir in full_dirs:
                indir   = full_dir.name
                entries = sorted(
                    e for e in full_dir.iterdir()
                    if e.is_file() and e.name.isdigit() and len(e.name) == 6
                )
                for entry in entries:
                    incset = entry.name
                    bktype = "FULL" if incset == "000000" else "INC "
                    mtime  = datetime.fromtimestamp(entry.stat().st_mtime)
                    size   = entry.stat().st_size
                    if size >= 1_000_000_000:
                        size_str = f"{size / 1_000_000_000:>7.2f} GB"
                    elif size >= 1_000_000:
                        size_str = f"{size / 1_000_000:>7.2f} MB"
                    else:
                        size_str = f"{size:>10}  B"
                    points.append({
                        'date':      mtime.strftime('%d-%m-%Y %H:%M'),
                        'type':      bktype,
                        'restpoint': f"{indir}-{incset}",
                        'size':      size_str,
                        'image':     image_dir.name,
                        'job':       job_name,
                        'job_path':  job_path,
                    })
    return points


def fetch_images_for_vm_job(jobs: list, vm_name: str, job_name: str) -> list:
    """Disk images of a VM for a given job, obtained from the filesystem."""
    for job in jobs:
        if job['name'] != job_name:
            continue
        vm_dir = Path(job['path']) / job_name / vm_name
        if not vm_dir.is_dir():
            return []
        return sorted(
            d.name for d in vm_dir.iterdir()
            if d.is_dir() and d.name not in SKIP_DIRS
        )
    return []


def vmdef_exists(point: dict, vm_name: str) -> bool:
    """Check whether the VM definition XML file exists for this restore point."""
    indir, incset = point['restpoint'].split('-')
    vmdef_path = (Path(point['job_path']) / point['job'] / vm_name /
                  "VMDEF" / indir / incset / f"{vm_name}.xml")
    return vmdef_path.exists()


# ── Logica di restore ─────────────────────────────────────────────────────────

def run_rbd_import(cmd_args, log_progress_fn):
    """
    Run rbd import-diff showing progress in real time.
    rbd uses \\r for progress lines (overwrites the same line)
    and \\n for error messages (permanent lines).
    Returns (returncode, list_of_error_lines).
    """
    proc = subprocess.Popen(cmd_args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    cur_progress = ['']
    errlines     = []

    def _reader():
        buf = b''
        while True:
            chunk = proc.stderr.read(64)
            if not chunk:
                break
            buf += chunk
            while buf:
                cr = buf.find(b'\r')
                nl = buf.find(b'\n')
                if cr == -1 and nl == -1:
                    break
                if cr != -1 and (nl == -1 or cr < nl):
                    text = buf[:cr].decode('utf-8', errors='replace').strip()
                    if text:
                        cur_progress[0] = text
                    buf = buf[cr + 1:]
                else:
                    text = buf[:nl].decode('utf-8', errors='replace').strip()
                    if text:
                        errlines.append(text)
                    buf = buf[nl + 1:]
        if buf:
            text = buf.decode('utf-8', errors='replace').strip()
            if text:
                errlines.append(text)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    while proc.poll() is None:
        if cur_progress[0]:
            log_progress_fn(f"  {cur_progress[0]}")
        time.sleep(0.15)

    t.join(timeout=3)
    if cur_progress[0]:
        log_progress_fn(f"  {cur_progress[0]}")

    return proc.returncode, errlines


def patch_xml(xml_content, vm_name, date_suffix, restored_images):
    """
    Patch the VM XML content:
    - rename the VM by appending the _rest-DATE suffix
    - remove the UUID
    - replace disk image references with restored names
    - disable network interfaces (link state=down, NIC still defined)
    """
    xml_content = re.sub(
        r'(<name>)' + re.escape(vm_name) + r'(</name>)',
        r'\g<1>' + vm_name + '_rest-' + date_suffix + r'\2',
        xml_content, count=1,
    )
    xml_content = re.sub(
        r'(<title>)' + re.escape(vm_name) + r'(</title>)',
        r'\g<1>' + vm_name + '_rest-' + date_suffix + r'\2',
        xml_content, count=1,
    )
    xml_content = re.sub(r'\s*<uuid>[^<]*</uuid>', '', xml_content)

    def _disable_iface(m):
        block = m.group(0)
        block = re.sub(r'\s*<link\b[^/]*/>', '', block)          # rimuovi <link> esistenti
        block = re.sub(r'(</interface>)',
                       r"  <link state='down'/>\n\1", block)      # aggiungi state=down
        return block

    xml_content = re.sub(
        r'<interface\b[^>]*>.*?</interface>',
        _disable_iface,
        xml_content,
        flags=re.DOTALL,
    )
    for orig, restored in restored_images.items():
        xml_content = xml_content.replace(f'/{orig}', f'/{restored}')
        xml_content = re.sub(
            r"(name=['\"])" + re.escape(orig) + r"(['\"])",
            r'\g<1>' + restored + r'\2',
            xml_content,
        )
    return xml_content


def do_restore(config, vm_name, point, images, log, log_progress):
    """
    Perform a full VM restore.
    images: list of images to restore (already selected by the user).
    log(msg, color): callback to update the on-screen log.
    Returns dict with 'restored_images', 'xml_dest', 'virsh_ok', 'error'.
    """
    poolname    = config.get('poolname', 'rbdpool01')
    job_name    = point['job']
    job_path    = point['job_path']
    restpoint   = point['restpoint']
    indir, incset = restpoint.split('-')
    point_int   = int(incset)
    date_suffix = datetime.now().strftime('%Y%m%d')

    if not images:
        return {'error': 'No image selected for restore.'}

    restored_images = {}

    # ── restore di ogni immagine disco ───────────────────────────────────────
    for image in images:
        backup_path = Path(job_path) / job_name / vm_name / image / indir
        if not backup_path.is_dir():
            log(f"  SKIP {image}: directory {backup_path} not found", C_ERR)
            continue

        restored_name = f"{image}_rest-{date_suffix}"
        log(f"Restoring {image} → {restored_name}", C_HEADER)

        cmd = ['rbd', 'create', restored_name, '--size', '1024', '-p', poolname]
        log(f"  $ {' '.join(cmd)}", C_DIM)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log(f"  ERROR: {r.stderr.strip()}", C_ERR)
            return {'error': f"rbd create failed for {image}: {r.stderr.strip()}"}
        log("  image created", C_OK)

        for x in range(point_int + 1):
            diff_file = str(x).zfill(6)
            diff_path = str(backup_path / diff_file)
            log(f"  import-diff .../{diff_file} → {poolname}/{restored_name}", C_DIM)
            rc, errlines = run_rbd_import(
                ['rbd', 'import-diff', diff_path, f"{poolname}/{restored_name}"],
                log_progress_fn=log_progress,
            )
            if rc != 0:
                for line in errlines:
                    log(f"  {line}", C_ERR)
                last_err = errlines[-1] if errlines else "unknown error"
                return {'error': f"import-diff failed ({diff_file}): {last_err}"}
        log("  OK", C_OK)
        restored_images[image] = restored_name

    if not restored_images:
        return {'error': 'No image restored successfully.'}

    # ── trova file XML definizione VM ─────────────────────────────────────────
    xml_src = None
    log("─" * 60)
    log("Searching VM definition XML:", C_HEADER)

    # 1) cerca in VMDEF del backup (path coerente con bkexec.py)
    vmdef_path = (Path(job_path) / job_name / vm_name / "VMDEF" /
                  indir / incset / f"{vm_name}.xml")
    log(f"  [1] {vmdef_path}")
    if vmdef_path.exists():
        xml_src = vmdef_path
        log("      found.", C_OK)
    else:
        log("      not found.")
        # 2) fallback to vm_config_path from config.ini
        vm_cfg = config.get('vm_config_path', '/etc/libvirt/qemu').rstrip('/')
        alt    = Path(vm_cfg) / f"{vm_name}.xml"
        log(f"  [2] {alt}")
        if alt.exists():
            xml_src = alt
            log("      found.", C_OK)
        else:
            log("      not found.", C_ERR)
            log("ERROR: cannot find the XML file.", C_ERR)
            return {
                'restored_images': restored_images,
                'xml_dest': None,
                'virsh_ok': False,
                'error': f"XML not found in:\n  {vmdef_path}\n  {alt}",
            }

    # ── patcha e salva XML ────────────────────────────────────────────────────
    TMP_DIR.mkdir(exist_ok=True)
    xml_dest = TMP_DIR / f"{vm_name}_rest-{date_suffix}.xml"
    xml_content = xml_src.read_text()
    xml_content = patch_xml(xml_content, vm_name, date_suffix, restored_images)
    xml_dest.write_text(xml_content)
    log(f"Patched XML saved to {xml_dest}", C_OK)

    # ── virsh define ──────────────────────────────────────────────────────────
    log("Running: virsh define ...", C_HEADER)
    r = subprocess.run(['virsh', 'define', str(xml_dest)], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ERROR virsh define: {r.stderr.strip()}", C_ERR)
        return {
            'restored_images': restored_images,
            'xml_dest':        str(xml_dest),
            'virsh_ok':        False,
            'error':           f"virsh define failed: {r.stderr.strip()}",
        }
    log(r.stdout.strip(), C_OK)

    # ── virsh start (network disattivato via XML) ──────────────────────────────
    vm_rest_name = f"{vm_name}_rest-{date_suffix}"
    log(f"Starting VM '{vm_rest_name}' (network disabled)...", C_HEADER)
    r = subprocess.run(['virsh', 'start', vm_rest_name], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ERROR virsh start: {r.stderr.strip()}", C_ERR)
        return {
            'restored_images': restored_images,
            'xml_dest':        str(xml_dest),
            'virsh_ok':        True,
            'virsh_start':     False,
            'error':           f"virsh start failed: {r.stderr.strip()}",
        }

    log(r.stdout.strip(), C_OK)
    return {
        'restored_images': restored_images,
        'xml_dest':        str(xml_dest),
        'virsh_ok':        True,
        'virsh_start':     True,
        'error':           None,
    }


# ── Widget: lista scrollabile ─────────────────────────────────────────────────

def scrollable_list(stdscr, title, items, header=None,
                    hint="↑↓ navigate   Enter select   q back"):
    cur_idx = 0
    top_idx = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        stdscr.box()
        t = f" {title} "
        stdscr.addstr(0, max(2, (w - len(t)) // 2),
                      t, curses.color_pair(C_TITLE) | curses.A_BOLD)

        header_lines = 0
        if header:
            stdscr.addstr(1, 2, header[:w - 4],
                          curses.color_pair(C_HEADER) | curses.A_BOLD)
            stdscr.hline(2, 1, curses.ACS_HLINE, w - 2)
            header_lines = 2

        list_start = 1 + header_lines
        list_rows  = h - 3 - header_lines

        try:
            stdscr.addstr(h - 1, 0, hint.ljust(w - 1)[:w - 1],
                          curses.color_pair(C_STATUS))
        except curses.error:
            pass

        if not items:
            stdscr.addstr(list_start, 2, "(no items)",
                          curses.color_pair(C_DIM) | curses.A_DIM)
        else:
            for i, item in enumerate(items[top_idx:top_idx + list_rows]):
                abs_i = top_idx + i
                y     = list_start + i
                label = str(item)[:w - 5]
                if abs_i == cur_idx:
                    stdscr.addstr(y, 2, f" {label:<{w - 5}}",
                                  curses.color_pair(C_SELECT) | curses.A_BOLD)
                else:
                    stdscr.addstr(y, 2, f" {label}")

            if len(items) > list_rows:
                pct = int(cur_idx / max(len(items) - 1, 1) * (list_rows - 1))
                for r in range(list_rows):
                    try:
                        stdscr.addch(list_start + r, w - 2,
                                     curses.ACS_BLOCK if r == pct else curses.ACS_VLINE)
                    except curses.error:
                        pass

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord('k')):
            if cur_idx > 0:
                cur_idx -= 1
                if cur_idx < top_idx:
                    top_idx = cur_idx
        elif key in (curses.KEY_DOWN, ord('j')):
            if cur_idx < len(items) - 1:
                cur_idx += 1
                if cur_idx >= top_idx + list_rows:
                    top_idx += 1
        elif key == curses.KEY_PPAGE:
            cur_idx = max(0, cur_idx - list_rows)
            top_idx = max(0, top_idx - list_rows)
        elif key == curses.KEY_NPAGE:
            cur_idx = min(len(items) - 1, cur_idx + list_rows)
            top_idx = min(max(0, len(items) - list_rows), top_idx + list_rows)
        elif key == curses.KEY_HOME:
            cur_idx = top_idx = 0
        elif key == curses.KEY_END:
            cur_idx = len(items) - 1
            top_idx = max(0, cur_idx - list_rows + 1)
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            if items:
                return cur_idx
        elif key in (ord('q'), ord('Q'), 27):
            return None


# ── Widget: messaggio modale ──────────────────────────────────────────────────

def show_message(stdscr, title, lines, color=None):
    h, w  = stdscr.getmaxyx()
    box_w = min(max((len(l) for l in lines), default=20) + 6, w - 4)
    box_h = min(len(lines) + 4, h - 4)
    box_y = (h - box_h) // 2
    box_x = (w - box_w) // 2
    win   = curses.newwin(box_h, box_w, box_y, box_x)
    win.box()
    t = f" {title} "
    win.addstr(0, max(2, (box_w - len(t)) // 2),
               t, curses.color_pair(C_TITLE) | curses.A_BOLD)
    for i, line in enumerate(lines[:box_h - 3]):
        attr = curses.color_pair(color) if color else curses.A_NORMAL
        try:
            win.addstr(i + 1, 2, line[:box_w - 4], attr)
        except curses.error:
            pass
    cont = " Press any key to continue "
    try:
        win.addstr(box_h - 1, max(2, (box_w - len(cont)) // 2),
                   cont, curses.color_pair(C_STATUS))
    except curses.error:
        pass
    win.refresh()
    stdscr.getch()


# ── Widget: finestra di log live ──────────────────────────────────────────────

class LogView:
    """Full-screen log window that updates line by line."""

    def __init__(self, stdscr, title):
        self.stdscr         = stdscr
        self.title          = title
        self.lines          = []
        self._last_progress = False
        self._draw_frame()

    def _draw_frame(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self.stdscr.box()
        t = f" {self.title} "
        self.stdscr.addstr(0, max(2, (w - len(t)) // 2),
                           t, curses.color_pair(C_TITLE) | curses.A_BOLD)
        hint = " Please wait... "
        try:
            self.stdscr.addstr(h - 1, 0, hint.ljust(w - 1)[:w - 1],
                               curses.color_pair(C_STATUS))
        except curses.error:
            pass
        self.stdscr.refresh()

    def log(self, msg, color=0):
        self._last_progress = False
        self.lines.append((msg, color))
        self._redraw()

    def log_progress(self, msg, color=C_DIM):
        """Update the last line in-place (for rbd progress)."""
        if self._last_progress and self.lines:
            self.lines[-1] = (msg, color)
        else:
            self.lines.append((msg, color))
            self._last_progress = True
        self._redraw()

    def _redraw(self):
        h, w      = self.stdscr.getmaxyx()
        list_rows = h - 3
        visible   = self.lines[-list_rows:]
        for i, (text, color) in enumerate(visible):
            y    = 1 + i
            attr = curses.color_pair(color) if color else curses.A_NORMAL
            try:
                self.stdscr.addstr(y, 2, text[:w - 4].ljust(w - 4), attr)
            except curses.error:
                pass
        self.stdscr.refresh()

    def done(self, success=True):
        h, w = self.stdscr.getmaxyx()
        if success:
            hint  = " Operation completed — press any key to continue "
            color = C_OK
        else:
            hint  = " Operation failed — press any key to continue "
            color = C_ERR
        try:
            self.stdscr.addstr(h - 1, 0, hint.ljust(w - 1)[:w - 1],
                               curses.color_pair(C_STATUS) | curses.A_BOLD)
        except curses.error:
            pass
        self.stdscr.refresh()
        self.stdscr.getch()


# ── Widget: dialogo di conferma ───────────────────────────────────────────────

def confirm_dialog(stdscr, title, lines):
    h, w  = stdscr.getmaxyx()
    box_w = min(max((len(l) for l in lines), default=30) + 6, w - 4)
    box_h = min(len(lines) + 5, h - 4)
    box_y = (h - box_h) // 2
    box_x = (w - box_w) // 2
    win   = curses.newwin(box_h, box_w, box_y, box_x)
    win.keypad(True)
    win.box()
    t = f" {title} "
    win.addstr(0, max(2, (box_w - len(t)) // 2),
               t, curses.color_pair(C_TITLE) | curses.A_BOLD)
    for i, line in enumerate(lines[:box_h - 4]):
        try:
            win.addstr(i + 1, 2, line[:box_w - 4])
        except curses.error:
            pass
    footer = " Enter/y = Confirm   q/n/Esc = Cancel "
    try:
        win.addstr(box_h - 1, max(2, (box_w - len(footer)) // 2),
                   footer, curses.color_pair(C_STATUS))
    except curses.error:
        pass
    win.refresh()
    while True:
        key = stdscr.getch()
        if key in (curses.KEY_ENTER, ord('\n'), ord('\r'), ord('y'), ord('Y')):
            return True
        if key in (ord('q'), ord('Q'), ord('n'), ord('N'), 27):
            return False


# ── Widget: selezione immagini disco ─────────────────────────────────────────

def select_images_dialog(stdscr, images: list, vmdef_missing: bool) -> list:
    """
    Image list with checkboxes.
    All selected by default; Space to toggle, Enter to confirm.
    Returns the list of selected images, or None if cancelled.
    """
    selected = [True] * len(images)
    cur_idx  = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.box()

        t = " Select images to restore "
        stdscr.addstr(0, max(2, (w - len(t)) // 2),
                      t, curses.color_pair(C_TITLE) | curses.A_BOLD)

        row = 1
        if vmdef_missing:
            warn = "  WARNING: VM XML definition not found for this restore point."
            try:
                stdscr.addstr(row, 0, warn[:w - 1],
                              curses.color_pair(C_ERR) | curses.A_BOLD)
            except curses.error:
                pass
            row += 1
            stdscr.hline(row, 1, curses.ACS_HLINE, w - 2)
            row += 1

        try:
            stdscr.addstr(row, 2,
                          "Images found in job (Space = toggle):"[:w - 4],
                          curses.color_pair(C_HEADER))
        except curses.error:
            pass
        row += 1
        stdscr.hline(row, 1, curses.ACS_HLINE, w - 2)
        row += 1

        list_start = row
        list_rows  = h - row - 2

        for i, img in enumerate(images[:list_rows]):
            y     = list_start + i
            check = "[x]" if selected[i] else "[ ]"
            label = f" {check} {img}"
            if i == cur_idx:
                try:
                    stdscr.addstr(y, 2, label[:w - 5].ljust(w - 5),
                                  curses.color_pair(C_SELECT) | curses.A_BOLD)
                except curses.error:
                    pass
            else:
                color = C_OK if selected[i] else C_DIM
                try:
                    stdscr.addstr(y, 2, label[:w - 5],
                                  curses.color_pair(color))
                except curses.error:
                    pass

        hint = " ↑↓ navigate   Space toggle   Enter confirm   q cancel "
        try:
            stdscr.addstr(h - 1, 0, hint.ljust(w - 1)[:w - 1],
                          curses.color_pair(C_STATUS))
        except curses.error:
            pass

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord('k')):
            cur_idx = max(0, cur_idx - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            cur_idx = min(len(images) - 1, cur_idx + 1)
        elif key == ord(' '):
            selected[cur_idx] = not selected[cur_idx]
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            chosen = [img for img, sel in zip(images, selected) if sel]
            if not chosen:
                show_message(stdscr, "Error",
                             ["Select at least one image."], C_ERR)
            else:
                return chosen
        elif key in (ord('q'), ord('Q'), 27):
            return None


# ── Schermate ─────────────────────────────────────────────────────────────────

def screen_restore_run(stdscr, config, vm_name, point, images):
    """Run the restore showing the live log."""
    date_suffix    = datetime.now().strftime('%Y%m%d')
    restored_names = [f"{img}_rest-{date_suffix}" for img in images]

    lv = LogView(stdscr, f"Restore — {vm_name} → {point['restpoint']}")
    lv.log(f"Job:           {point['job']}")
    lv.log(f"Restore point: {point['restpoint']}  ({point['date'].strip()})")
    lv.log(f"Images:        {', '.join(images)}")
    lv.log(f"Destination:   {', '.join(restored_names)}")
    lv.log("─" * 60)

    result = do_restore(config, vm_name, point, images, lv.log, lv.log_progress)

    lv.log("─" * 60)
    if result.get('error') and not result.get('restored_images'):
        lv.log(f"FAILED: {result['error']}", C_ERR)
        lv.done(success=False)
        return

    if result.get('virsh_ok'):
        vm_rest_name = f"{vm_name}_rest-{date_suffix}"
        lv.log(f"VM '{vm_rest_name}' defined successfully.", C_OK)
        lv.log(f"XML:  {result['xml_dest']}", C_DIM)
        if result.get('virsh_start'):
            lv.log("VM started with network DISABLED.", C_OK)
            lv.log("Re-enable network with:", C_DIM)
            lv.log(f"  virsh domif-setlink {vm_rest_name} <iface> up", C_DIM)
        else:
            lv.log(f"WARNING: VM start failed. {result.get('error', '')}", C_ERR)
            lv.log(f"Start manually: virsh start {vm_rest_name}", C_DIM)
        lv.done(success=result.get('virsh_start', False))
    else:
        lv.log(f"Images restored: {', '.join(result['restored_images'].values())}", C_OK)
        if result.get('error'):
            lv.log(f"WARNING: {result['error']}", C_ERR)
        if result.get('xml_dest'):
            lv.log(f"XML saved to: {result['xml_dest']}", C_DIM)
            lv.log("Run manually: virsh define " + result['xml_dest'], C_DIM)
        lv.done(success=False)


def screen_restore_points(stdscr, config, jobs, vm_name):
    h, w = stdscr.getmaxyx()
    try:
        stdscr.addstr(h - 1, 0,
                      f" Loading restore points for {vm_name}...".ljust(w - 1)[:w - 1],
                      curses.color_pair(C_STATUS))
    except curses.error:
        pass
    stdscr.refresh()

    points = fetch_restore_points(jobs, vm_name)
    if not points:
        show_message(stdscr, "Restore point",
                     [f"No restore points found for '{vm_name}'."])
        return

    col_date = 16
    col_type =  5
    col_rp   = 14
    col_size = 11
    col_img  = 20
    header = (f"{'DATE':<{col_date}}  {'TYPE':<{col_type}}  "
              f"{'RESTORE POINT':<{col_rp}}  {'SIZE':>{col_size}}  "
              f"{'IMAGE':<{col_img}}  JOB")
    labels = [
        (f"{p['date']:<{col_date}}  {p['type']:<{col_type}}  "
         f"{p['restpoint']:<{col_rp}}  {p['size']:>{col_size}}  "
         f"{p['image']:<{col_img}}  {p['job']}")
        for p in points
    ]

    while True:
        idx = scrollable_list(
            stdscr,
            title=f"Restore point — {vm_name}",
            items=labels,
            header=header,
            hint="↑↓ navigate   Enter restore   q back",
        )
        if idx is None:
            return

        point       = points[idx]
        date_suffix = datetime.now().strftime('%Y%m%d')
        all_images  = fetch_images_for_vm_job(jobs, vm_name, point['job'])

        if not all_images:
            show_message(stdscr, "Error",
                         [f"No images found for {vm_name} / {point['job']}."],
                         C_ERR)
            continue

        # Selezione immagini — sempre mostrata, con warning se VMDEF mancante
        has_vmdef = vmdef_exists(point, vm_name)
        images    = select_images_dialog(stdscr, all_images, vmdef_missing=not has_vmdef)
        if images is None:
            continue

        info_lines = [
            f"VM:            {vm_name}",
            f"Job:           {point['job']}",
            f"Restore point: {point['restpoint']}  ({point['date'].strip()})",
            "",
            "Images that will be restored:",
        ] + [
            f"  {img}  →  {img}_rest-{date_suffix}"
            for img in images
        ] + [
            "",
            f"Pool: {config.get('poolname', 'rbdpool01')}",
        ]
        if not has_vmdef:
            info_lines += ["", "WARNING: no XML found — virsh define will not be run."]

        if confirm_dialog(stdscr, "Confirm restore", info_lines):
            screen_restore_run(stdscr, config, vm_name, point, images)


def screen_vm_list(stdscr, config, jobs):
    h, w = stdscr.getmaxyx()
    try:
        stdscr.addstr(h - 1, 0,
                      " Loading VM list...".ljust(w - 1)[:w - 1],
                      curses.color_pair(C_STATUS))
    except curses.error:
        pass
    stdscr.refresh()

    vms = fetch_vms(jobs)
    if not vms:
        show_message(stdscr, "Restore", ["No VMs found in job directories."])
        return

    while True:
        idx = scrollable_list(
            stdscr,
            title="Restore — Select VM",
            items=vms,
        )
        if idx is None:
            return
        screen_restore_points(stdscr, config, jobs, vms[idx])


# ── Widget: input testuale modale ────────────────────────────────────────────

def edit_text_dialog(stdscr, title: str, current: str) -> str:
    """
    Modal text input. Returns the new value (string) or None if cancelled.
    Left/right arrows and Backspace/Delete supported.
    """
    h, w   = stdscr.getmaxyx()
    box_w  = min(70, w - 4)
    box_h  = 6
    box_y  = (h - box_h) // 2
    box_x  = (w - box_w) // 2
    win    = curses.newwin(box_h, box_w, box_y, box_x)
    win.keypad(True)

    buf = list(current)
    cur = len(buf)

    while True:
        win.erase()
        win.box()
        t = f" {title} "
        win.addstr(0, max(2, (box_w - len(t)) // 2),
                   t, curses.color_pair(C_TITLE) | curses.A_BOLD)
        win.addstr(2, 2, "Value:", curses.color_pair(C_HEADER))
        footer = " Enter confirm   Esc cancel "
        try:
            win.addstr(box_h - 1, max(2, (box_w - len(footer)) // 2),
                       footer, curses.color_pair(C_STATUS))
        except curses.error:
            pass

        field_w = box_w - 6
        text    = ''.join(buf)
        # scroll the view to keep cursor visible
        start   = max(0, cur - field_w + 1)
        display = text[start:start + field_w]
        cur_x   = cur - start + 3

        try:
            win.addstr(3, 3, display.ljust(field_w),
                       curses.color_pair(C_SELECT))
            curses.curs_set(1)
            win.move(3, min(cur_x, box_w - 3))
        except curses.error:
            pass

        win.refresh()
        key = win.getch()

        if key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            curses.curs_set(0)
            return ''.join(buf)
        elif key == 27:
            curses.curs_set(0)
            return None
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if cur > 0:
                buf.pop(cur - 1)
                cur -= 1
        elif key == curses.KEY_DC:
            if cur < len(buf):
                buf.pop(cur)
        elif key == curses.KEY_LEFT:
            cur = max(0, cur - 1)
        elif key == curses.KEY_RIGHT:
            cur = min(len(buf), cur + 1)
        elif key == curses.KEY_HOME:
            cur = 0
        elif key == curses.KEY_END:
            cur = len(buf)
        elif 32 <= key <= 126:
            buf.insert(cur, chr(key))
            cur += 1


# ── Widget: selezione multipla generica ──────────────────────────────────────

def edit_multiselect_dialog(stdscr, title: str, items: list,
                             selected: list) -> list:
    """
    Generic checklist. `items` = list of strings to display.
    `selected` = list of pre-selected indices (0-based).
    Returns list of selected indices, or None if cancelled.
    """
    state   = [i in selected for i in range(len(items))]
    cur_idx = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.box()

        t = f" {title} "
        stdscr.addstr(0, max(2, (w - len(t)) // 2),
                      t, curses.color_pair(C_TITLE) | curses.A_BOLD)

        list_start = 2
        list_rows  = h - 4

        for i, label in enumerate(items[:list_rows]):
            y     = list_start + i
            check = "[x]" if state[i] else "[ ]"
            line  = f" {check} {label}"
            if i == cur_idx:
                try:
                    stdscr.addstr(y, 2, line[:w - 5].ljust(w - 5),
                                  curses.color_pair(C_SELECT) | curses.A_BOLD)
                except curses.error:
                    pass
            else:
                color = C_OK if state[i] else C_DIM
                try:
                    stdscr.addstr(y, 2, line[:w - 5],
                                  curses.color_pair(color))
                except curses.error:
                    pass

        hint = " ↑↓ navigate   Space toggle   Enter confirm   q cancel "
        try:
            stdscr.addstr(h - 1, 0, hint.ljust(w - 1)[:w - 1],
                          curses.color_pair(C_STATUS))
        except curses.error:
            pass

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord('k')):
            cur_idx = max(0, cur_idx - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            cur_idx = min(len(items) - 1, cur_idx + 1)
        elif key == ord(' '):
            state[cur_idx] = not state[cur_idx]
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            return [i for i, s in enumerate(state) if s]
        elif key in (ord('q'), ord('Q'), 27):
            return None


# ── Schermata: editing job ────────────────────────────────────────────────────

DAY_NAMES   = ["Monday", "Tuesday", "Wednesday", "Thursday",
               "Friday", "Saturday", "Sunday"]
DAY_SHORT   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEK_NAMES  = ["Week 1", "Week 2", "Week 3",
               "Week 4", "Week 5"]

# Editable fields: (json key, label, type)
# types: 'bool', 'str', 'int', 'days', 'weeks', 'strlist'
JOB_FIELDS = [
    ("enabled",     "Enabled",             "bool"),
    ("path",        "Backup path",         "str"),
    ("mountpoint",  "Mountpoint",          "str"),
    ("checkmount",  "Check mount",         "bool"),
    ("max_inc",     "Max incrementals",    "int"),
    ("snap-prefix", "Snapshot prefix",     "str"),
    ("max-snaps",   "Max snapshots",       "int"),
    ("_days",       "Scheduled days",      "days"),
    ("_weeks",      "Scheduled weeks",     "weeks"),
    ("email_from",  "Sender email",        "str"),
    ("rcpt_to",     "Email recipients",    "strlist"),
]


def job_field_display(job: dict, ftype: str, fkey: str) -> str:
    """Return the textual representation of a field value."""
    if fkey == "_days":
        days = job.get("schedule", {}).get("days", [])
        return "  ".join(DAY_SHORT[d - 1] for d in sorted(days)) or "(none)"
    if fkey == "_weeks":
        weeks = job.get("schedule", {}).get("weeks", [])
        return "  ".join(str(w) for w in sorted(weeks)) or "(none)"
    val = job.get(fkey)
    if ftype == "bool":
        return "YES" if val else "NO"
    if ftype == "strlist":
        return ", ".join(val) if isinstance(val, list) else str(val or "")
    return str(val) if val is not None else ""


def screen_job_edit(stdscr, config: dict, jobs: list, job_idx: int):
    job     = jobs[job_idx]
    cur_idx = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.box()

        title = f" Job: {job['name']} "
        stdscr.addstr(0, max(2, (w - len(title)) // 2),
                      title, curses.color_pair(C_TITLE) | curses.A_BOLD)

        # riga info readonly
        en_str   = "ENABLED" if job.get("enabled") else "DISABLED"
        en_color = C_OK if job.get("enabled") else C_ERR
        info     = (f"  Last backup: {job.get('lastrun', 'never')}   "
                    f"Completed: {job.get('lastcompletion', 'never')}")
        try:
            stdscr.addstr(1, 2, en_str, curses.color_pair(en_color) | curses.A_BOLD)
            stdscr.addstr(1, 2 + len(en_str), info[:w - 2 - len(en_str)])
        except curses.error:
            pass
        stdscr.hline(2, 1, curses.ACS_HLINE, w - 2)

        list_start = 3
        list_rows  = h - 5
        label_w    = 22

        for i, (fkey, flabel, ftype) in enumerate(JOB_FIELDS[:list_rows]):
            y     = list_start + i
            value = job_field_display(job, ftype, fkey)
            line  = f" {flabel:<{label_w}} {value}"
            if i == cur_idx:
                try:
                    stdscr.addstr(y, 2, line[:w - 5].ljust(w - 5),
                                  curses.color_pair(C_SELECT) | curses.A_BOLD)
                except curses.error:
                    pass
            else:
                try:
                    stdscr.addstr(y, 2, f" {flabel:<{label_w}}",
                                  curses.color_pair(C_HEADER))
                    stdscr.addstr(y, 2 + 1 + label_w + 1, value[:w - label_w - 8])
                except curses.error:
                    pass

        hint = " ↑↓ navigate   Enter edit   v VMs   s save   q back "
        try:
            stdscr.addstr(h - 1, 0, hint.ljust(w - 1)[:w - 1],
                          curses.color_pair(C_STATUS))
        except curses.error:
            pass

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord('k')):
            cur_idx = max(0, cur_idx - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            cur_idx = min(len(JOB_FIELDS) - 1, cur_idx + 1)

        elif key in (ord('v'), ord('V')):
            screen_vm_list_for_job(stdscr, config, job)

        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            fkey, flabel, ftype = JOB_FIELDS[cur_idx]

            if ftype == "bool":
                if fkey == "checkmount":
                    job["checkmount"] = 0 if job.get("checkmount") else 1
                else:
                    job[fkey] = 0 if job.get(fkey) else 1

            elif ftype in ("str", "int"):
                current = str(job.get(fkey, ""))
                new_val = edit_text_dialog(stdscr, flabel, current)
                if new_val is not None:
                    job[fkey] = int(new_val) if ftype == "int" else new_val

            elif ftype == "strlist":
                current = ", ".join(job.get(fkey, []))
                new_val = edit_text_dialog(stdscr, flabel, current)
                if new_val is not None:
                    job[fkey] = [v.strip() for v in new_val.split(",") if v.strip()]

            elif ftype == "days":
                current_days = job.get("schedule", {}).get("days", [])
                sel = edit_multiselect_dialog(
                    stdscr, "Scheduled days",
                    DAY_NAMES, [d - 1 for d in current_days]
                )
                if sel is not None:
                    job.setdefault("schedule", {})["days"] = [i + 1 for i in sel]

            elif ftype == "weeks":
                current_weeks = job.get("schedule", {}).get("weeks", [])
                sel = edit_multiselect_dialog(
                    stdscr, "Scheduled weeks",
                    WEEK_NAMES, [w - 1 for w in current_weeks]
                )
                if sel is not None:
                    job.setdefault("schedule", {})["weeks"] = [i + 1 for i in sel]

        elif key in (ord('s'), ord('S')):
            jobs[job_idx] = job
            save_jobs(jobs)
            show_message(stdscr, "Saved",
                         [f"Job '{job['name']}' saved to backupjobs.json."], C_OK)

        elif key in (ord('q'), ord('Q'), 27):
            return


# ── Helpers VM ───────────────────────────────────────────────────────────────

def load_vmbackup(vm_dir: Path) -> dict:
    path = vm_dir / "vmbackup.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"lastrun": None, "success": None}


def get_vms_in_job(job: dict) -> list:
    """VMs already present in the job directory (subdirs, excluding LOGS)."""
    job_dir = Path(job["path"]) / job["name"]
    if not job_dir.is_dir():
        return []
    return sorted(
        d.name for d in job_dir.iterdir()
        if d.is_dir() and d.name not in SKIP_DIRS
    )


def get_available_vms(config: dict, job: dict) -> list:
    """
    VMs available to add: they have a definition XML file in
    vm_config_path but are not yet present in the job directory.
    """
    vm_cfg_path = Path(config.get("vm_config_path", "/etc/libvirt/qemu"))
    existing    = set(get_vms_in_job(job))
    available   = []
    if vm_cfg_path.is_dir():
        for xml_file in sorted(vm_cfg_path.glob("*.xml")):
            vm_name = xml_file.stem
            if vm_name not in existing:
                available.append(vm_name)
    return available


# ── Schermata: VM associate al job ────────────────────────────────────────────

def screen_add_vm(stdscr, config: dict, job: dict):
    available = get_available_vms(config, job)
    if not available:
        show_message(stdscr, "Add VM",
                     ["No VMs available.",
                      "(all already present or no XML file found)"])
        return

    while True:
        idx = scrollable_list(
            stdscr,
            title=f"Add VM — {job['name']}",
            items=available,
            hint="↑↓ navigate   Enter add   q back",
        )
        if idx is None:
            return

        vm_name = available[idx]
        vm_dir  = Path(job["path"]) / job["name"] / vm_name

        if not confirm_dialog(stdscr, "Confirm",
                              [f"Add VM '{vm_name}' to job '{job['name']}'?",
                               f"The following directory will be created:",
                               f"  {vm_dir}"]):
            continue

        try:
            vm_dir.mkdir(parents=True, exist_ok=True)
            (vm_dir / "vmbackup.json").write_text(
                json.dumps({"lastrun": None, "success": None}, indent=2)
            )
            show_message(stdscr, "VM added",
                         [f"'{vm_name}' added to job '{job['name']}'.",
                          f"Directory: {vm_dir}"], C_OK)
            available.pop(idx)
            if not available:
                return
        except Exception as e:
            show_message(stdscr, "Error", [str(e)], C_ERR)


def screen_vm_list_for_job(stdscr, config: dict, job: dict):
    col_name = 24
    col_run  = 20
    header   = f"{'VM':<{col_name}}  {'LAST BACKUP':<{col_run}}  STATUS"

    while True:
        vms    = get_vms_in_job(job)
        labels = []
        for vm_name in vms:
            vm_dir = Path(job["path"]) / job["name"] / vm_name
            vmb    = load_vmbackup(vm_dir)
            run    = vmb.get("lastrun") or "never"
            ok     = vmb.get("success")
            stato  = "-" if ok is None else ("OK" if ok else "FAIL")
            labels.append(f"{vm_name:<{col_name}}  {run:<{col_run}}  {stato}")

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.box()

        t = f" VM — {job['name']} "
        stdscr.addstr(0, max(2, (w - len(t)) // 2),
                      t, curses.color_pair(C_TITLE) | curses.A_BOLD)
        try:
            stdscr.addstr(1, 2, header[:w - 4],
                          curses.color_pair(C_HEADER) | curses.A_BOLD)
            stdscr.hline(2, 1, curses.ACS_HLINE, w - 2)
        except curses.error:
            pass

        list_start = 3
        list_rows  = h - 5

        if not labels:
            try:
                stdscr.addstr(list_start, 2, "(no VMs present)",
                              curses.color_pair(C_DIM))
            except curses.error:
                pass
        else:
            for i, label in enumerate(labels[:list_rows]):
                try:
                    stdscr.addstr(list_start + i, 2, label[:w - 4])
                except curses.error:
                    pass

        hint = " a add VM   q back "
        try:
            stdscr.addstr(h - 1, 0, hint.ljust(w - 1)[:w - 1],
                          curses.color_pair(C_STATUS))
        except curses.error:
            pass

        stdscr.refresh()
        key = stdscr.getch()

        if key in (ord('a'), ord('A')):
            screen_add_vm(stdscr, config, job)
        elif key in (ord('q'), ord('Q'), 27):
            return


# ── Schermata: lista backup job ───────────────────────────────────────────────

def screen_backup_list(stdscr, config: dict, jobs: list):
    while True:
        # ricarica ad ogni iterazione per riflettere modifiche appena salvate
        col_name  = 20
        col_en    =  3
        col_run   = 19
        col_compl = 19

        header = (f"{'JOB':<{col_name}}  {'EN':<{col_en}}  "
                  f"{'LAST BACKUP':<{col_run}}  {'COMPLETED':<{col_compl}}  PATH")

        labels = []
        for job in jobs:
            en   = "YES" if job.get("enabled") else "NO "
            run  = job.get("lastrun",        "never")
            comp = job.get("lastcompletion", "never")
            labels.append(
                f"{job['name']:<{col_name}}  {en:<{col_en}}  "
                f"{run:<{col_run}}  {comp:<{col_compl}}  {job.get('path','')}"
            )
        labels.append("  [+ Add new job]")

        idx = scrollable_list(
            stdscr,
            title="Backup — Configured jobs",
            items=labels,
            header=header,
            hint="↑↓ navigate   Enter edit job   q back",
        )
        if idx is None:
            return
        if idx == len(jobs):
            name = edit_text_dialog(stdscr, "New job name", "")
            if not name:
                continue
            new_job = {
                "name":        name,
                "enabled":     0,
                "path":        "",
                "mountpoint":  "",
                "checkmount":  1,
                "max_inc":     5,
                "snap-prefix": "BK",
                "max-snaps":   10,
                "email_from":  "",
                "rcpt_to":     [],
                "schedule":    {"days": [], "weeks": []},
            }
            jobs.append(new_job)
            save_jobs(jobs)
            screen_job_edit(stdscr, config, jobs, len(jobs) - 1)
        else:
            screen_job_edit(stdscr, config, jobs, idx)


def screen_main_menu(stdscr, config, jobs):
    ITEMS = ["  Backup", "  Restore"]
    while True:
        idx = scrollable_list(
            stdscr,
            title="CephBackup — Main menu",
            items=ITEMS,
            hint="↑↓ navigate   Enter select   q quit",
        )
        if idx is None:
            return
        if idx == 0:
            screen_backup_list(stdscr, config, jobs)
        elif idx == 1:
            screen_vm_list(stdscr, config, jobs)


# ── Entry point ───────────────────────────────────────────────────────────────

def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    init_colors()

    try:
        config = load_config()
    except Exception as e:
        stdscr.addstr(0, 0, f"Error reading config.ini: {e}")
        stdscr.getch()
        return

    try:
        jobs = load_jobs()
    except Exception as e:
        stdscr.addstr(0, 0, f"Error reading backupjobs.json: {e}")
        stdscr.getch()
        return

    screen_main_menu(stdscr, config, jobs)


if __name__ == '__main__':
    curses.wrapper(main)
