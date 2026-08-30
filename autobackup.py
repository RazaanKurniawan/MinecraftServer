#!/usr/bin/env python3
"""
Auto Backup System for File Changes
Supports:
- Linux native inotify real-time event watcher (zero CPU overhead) + Polling fallback
- Git automated version control (diffs, commit history, rollback)
- File snapshot backups in .backups/ with retention pruning
- Debounce mechanism for rapid batch edits
- Daemon management (start, stop, restart, status, history, diff, restore, backup-now)
"""

import os
import sys
import time
import json
import fnmatch
import shutil
import signal
import subprocess
import threading
import ctypes
import struct
from datetime import datetime
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "autobackup.json"
PID_FILE = BASE_DIR / "autobackup.pid"
LOG_FILE = BASE_DIR / "autobackup.log"

# Default Configuration
DEFAULT_CONFIG = {
    "backup_mode": "both",  # "both", "git", or "snapshot"
    "backup_dir": ".backups",
    "debounce_seconds": 3,
    "max_snapshot_history": 50,
    "git_enabled": True,
    "snapshot_enabled": True,
    "git_author_name": "AutoBackup Bot",
    "git_author_email": "autobackup@server.local",
    "ignored_patterns": [
        "logs/**",
        "*.log",
        "*.log.gz",
        ".cache/**",
        "cache/**",
        "libraries/**",
        "versions/**",
        ".paper/**",
        "*.jar",
        "world/**",
        "world_nether/**",
        "world_the_end/**",
        ".backups/**",
        ".git/**",
        "autobackup.pid",
        "autobackup.log",
        "*.tmp",
        "*.temp",
        ".DS_Store"
    ]
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_config = json.load(f)
                config = {**DEFAULT_CONFIG, **user_config}
                return config
        except Exception as e:
            log(f"Warning: Failed to parse {CONFIG_FILE.name}: {e}. Using defaults.")
    return DEFAULT_CONFIG.copy()

def log(msg, to_file=True):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    if to_file:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
        except Exception:
            pass

def is_ignored(rel_path, ignored_patterns):
    # Normalize path
    rel_path_str = str(rel_path).replace("\\", "/")
    if rel_path_str.startswith("./"):
        rel_path_str = rel_path_str[2:]
        
    for pattern in ignored_patterns:
        pat = pattern.replace("\\", "/")
        # Match directory or file pattern
        if fnmatch.fnmatch(rel_path_str, pat):
            return True
        if pat.endswith("/**") and (rel_path_str == pat[:-3] or rel_path_str.startswith(pat[:-2])):
            return True
        if fnmatch.fnmatch(os.path.basename(rel_path_str), pat):
            return True
    return False

# ==========================================
# Git Handler
# ==========================================
class GitHandler:
    def __init__(self, base_dir, config):
        self.base_dir = base_dir
        self.config = config
        self._init_git_if_needed()

    def _run_git(self, *args, check=True):
        cmd = ["git", "-c", "safe.directory=*"] + list(args)
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = self.config.get("git_author_name", "AutoBackup Bot")
        env["GIT_AUTHOR_EMAIL"] = self.config.get("git_author_email", "autobackup@server.local")
        env["GIT_COMMITTER_NAME"] = self.config.get("git_author_name", "AutoBackup Bot")
        env["GIT_COMMITTER_EMAIL"] = self.config.get("git_author_email", "autobackup@server.local")
        
        result = subprocess.run(
            cmd,
            cwd=str(self.base_dir),
            capture_output=True,
            text=True,
            env=env
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"Git command failed ({' '.join(cmd)}): {result.stderr.strip()}")
        return result

    def _init_git_if_needed(self):
        git_dir = self.base_dir / ".git"
        if not git_dir.exists():
            log("[Git] Inisialisasi git repository...")
            self._run_git("init")
        
        # Ensure default branch name
        target_branch = self.config.get("git_branch", "main")
        self._run_git("branch", "-M", target_branch, check=False)

        # Configure user locally
        self._run_git("config", "user.name", self.config.get("git_author_name", "AutoBackup Bot"), check=False)
        self._run_git("config", "user.email", self.config.get("git_author_email", "autobackup@server.local"), check=False)
        self._run_git("config", "core.autocrlf", "input", check=False)
        
        # Configure remote if set
        remote_url = self.config.get("git_remote_url", "").strip()
        if remote_url:
            self.set_remote(remote_url)

        # Check if HEAD exists (first baseline commit)
        try:
            head_check = self._run_git("rev-parse", "--verify", "HEAD", check=False)
            if head_check.returncode != 0:
                self._run_git("add", "-A")
                res = self._run_git("status", "--porcelain")
                if res.stdout.strip():
                    self._run_git("commit", "-m", "Initial commit baseline (AutoBackup)")
                    log("[Git] Initial baseline commit berhasil dibuat.")
        except Exception as e:
            log(f"[Git] Error creating initial commit: {e}")

    def set_remote(self, remote_url, remote_name="origin"):
        try:
            check_remote = self._run_git("remote", "get-url", remote_name, check=False)
            if check_remote.returncode == 0:
                self._run_git("remote", "set-url", remote_name, remote_url)
            else:
                self._run_git("remote", "add", remote_name, remote_url)
            log(f"[Git Remote] Remote '{remote_name}' diarahkan ke repository GitHub.")
            return True
        except Exception as e:
            log(f"[Git Remote] Error configuring remote: {e}")
            return False

    def push(self, remote_name="origin", branch=None):
        if not branch:
            branch = self.config.get("git_branch", "main")
        try:
            # Mask token in logs for security
            log(f"[Git Push] Mendorong commit ke GitHub ({remote_name}/{branch})...")
            res = self._run_git("push", "-u", remote_name, f"HEAD:{branch}", check=False)
            if res.returncode == 0:
                log(f"[Git Push] ✓ Sukses push ke GitHub ({remote_name}/{branch})!")
                return True
            else:
                err_msg = res.stderr.strip()
                log(f"[Git Push] ✗ Gagal push: {err_msg}")
                return False
        except Exception as e:
            log(f"[Git Push] Exception during push: {e}")
            return False

    def commit_changes(self, changed_files=None):
        try:
            status = self._run_git("status", "--porcelain")
            if not status.stdout.strip():
                return None  # No changes to commit

            self._run_git("add", "-A")
            
            # Check again after staging
            diff_check = self._run_git("diff", "--cached", "--name-only")
            staged_files = [line.strip() for line in diff_check.stdout.strip().splitlines() if line.strip()]
            if not staged_files:
                return None

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            file_count = len(staged_files)
            
            commit_title = f"Auto backup: {now_str} ({file_count} file{'s' if file_count > 1 else ''} changed)"
            
            details = "\n".join([f"- {f}" for f in staged_files[:20]])
            if file_count > 20:
                details += f"\n... and {file_count - 20} more files"
                
            commit_body = f"\n\nPerubahan terdeteksi:\n{details}"
            full_msg = commit_title + commit_body

            res = self._run_git("commit", "-m", full_msg)
            # Extract commit hash
            rev_parse = self._run_git("rev-parse", "--short", "HEAD")
            commit_hash = rev_parse.stdout.strip()
            log(f"[Git] Commit berhasil: {commit_hash} ({commit_title})")

            # Auto Push to GitHub
            if self.config.get("git_auto_push", False):
                remote_url = self.config.get("git_remote_url", "").strip()
                if remote_url:
                    self.push()

            return commit_hash
        except Exception as e:
            log(f"[Git] Error during commit: {e}")
            return None

    def get_history(self, limit=15):
        try:
            res = self._run_git("log", f"-n{limit}", "--pretty=format:%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M:%S")
            commits = []
            if res.stdout.strip():
                for line in res.stdout.strip().splitlines():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        commits.append({
                            "hash": parts[0],
                            "date": parts[1],
                            "message": parts[2]
                        })
            return commits
        except Exception as e:
            log(f"[Git] Error fetching history: {e}")
            return []

    def get_diff(self, commit_hash=None):
        try:
            if commit_hash:
                res = self._run_git("show", commit_hash, "--stat", "-p")
            else:
                res = self._run_git("diff", "HEAD~1..HEAD", "--stat", "-p")
            return res.stdout
        except Exception as e:
            return f"Error getting diff: {e}"

    def rollback(self, commit_hash):
        try:
            # 1. Remove files that were added after the target commit
            diff_added = self._run_git("diff", "--name-only", "--diff-filter=A", f"{commit_hash}..HEAD", check=False)
            if diff_added.returncode == 0 and diff_added.stdout.strip():
                for f_rel in diff_added.stdout.strip().splitlines():
                    f_rel = f_rel.strip()
                    if not f_rel:
                        continue
                    p = self.base_dir / f_rel
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                    elif p.is_dir():
                        shutil.rmtree(p)

            # 2. Checkout all tracked files from the target commit
            self._run_git("checkout", commit_hash, "--", ".")
            log(f"[Git] Sukses me-restore workspace ke versi commit: {commit_hash}")
            
            # 3. Create a commit recording the restoration
            self.commit_changes()
            return True
        except Exception as e:
            log(f"[Git] Gagal restore ke {commit_hash}: {e}")
            return False

# ==========================================
# Snapshot Handler
# ==========================================
class SnapshotHandler:
    def __init__(self, base_dir, config):
        self.base_dir = base_dir
        self.config = config
        self.backup_dir = self.base_dir / config.get("backup_dir", ".backups")
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_snapshot(self, changed_files, commit_hash=None):
        if not changed_files:
            return None

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        snap_id = f"snap_{timestamp}"
        snap_folder = self.backup_dir / snap_id
        snap_folder.mkdir(parents=True, exist_ok=True)

        copied_count = 0
        copied_files = []

        for rel_file in changed_files:
            src_file = self.base_dir / rel_file
            if src_file.is_file():
                dest_file = snap_folder / rel_file
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src_file, dest_file)
                    copied_count += 1
                    copied_files.append(str(rel_file))
                except Exception as e:
                    log(f"[Snapshot] Gagal menyalin file {rel_file}: {e}")

        if copied_count == 0:
            try:
                shutil.rmtree(snap_folder)
            except Exception:
                pass
            return None

        metadata = {
            "id": snap_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "git_commit": commit_hash,
            "files_count": copied_count,
            "files": copied_files
        }

        try:
            with open(snap_folder / "metadata.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            log(f"[Snapshot] Error writing snapshot metadata: {e}")

        log(f"[Snapshot] Snapshot berhasil disimpan: {snap_id} ({copied_count} files)")
        self._prune_old_snapshots()
        return snap_id

    def _prune_old_snapshots(self):
        max_snaps = int(self.config.get("max_snapshot_history", 50))
        try:
            snaps = []
            for item in self.backup_dir.iterdir():
                if item.is_dir() and item.name.startswith("snap_"):
                    snaps.append(item)
            
            # Sort by folder name (which contains timestamp YYYY-MM-DD_HH-MM-SS)
            snaps.sort(key=lambda p: p.name)
            
            if len(snaps) > max_snaps:
                to_delete = snaps[:len(snaps) - max_snaps]
                for old_snap in to_delete:
                    try:
                        shutil.rmtree(old_snap)
                        log(f"[Snapshot] Pruning snapshot lama: {old_snap.name}")
                    except Exception as e:
                        log(f"[Snapshot] Gagal menghapus {old_snap.name}: {e}")
        except Exception as e:
            log(f"[Snapshot] Error pruning snapshots: {e}")

    def list_snapshots(self, limit=15):
        snaps = []
        if not self.backup_dir.exists():
            return snaps
        for item in sorted(self.backup_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if item.is_dir() and item.name.startswith("snap_"):
                meta_file = item / "metadata.json"
                if meta_file.exists():
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                            snaps.append(meta)
                    except Exception:
                        snaps.append({"id": item.name, "timestamp": "Unknown", "files_count": 0})
                else:
                    snaps.append({"id": item.name, "timestamp": "Unknown", "files_count": 0})
                if len(snaps) >= limit:
                    break
        return snaps

    def restore_snapshot(self, snap_id):
        snap_folder = self.backup_dir / snap_id
        if not snap_folder.exists():
            log(f"[Snapshot] Snapshot {snap_id} tidak ditemukan.")
            return False

        meta_file = snap_folder / "metadata.json"
        restored = 0
        try:
            for root, _, files in os.walk(snap_folder):
                for file in files:
                    if file == "metadata.json":
                        continue
                    src = Path(root) / file
                    rel = src.relative_to(snap_folder)
                    dest = self.base_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    restored += 1
            log(f"[Snapshot] Sukses me-restore {restored} file dari {snap_id}")
            return True
        except Exception as e:
            log(f"[Snapshot] Gagal restore dari {snap_id}: {e}")
            return False

# ==========================================
# Linux Native Inotify Watcher (using ctypes)
# ==========================================
class InotifyWatcher:
    # inotify constants
    IN_MODIFY = 0x00000002
    IN_ATTRIB = 0x00000004
    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_FROM = 0x00000040
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100
    IN_DELETE = 0x00000200
    IN_DELETE_SELF = 0x00000400
    IN_MOVE_SELF = 0x00000800
    IN_ISDIR = 0x40000000

    WATCH_MASK = (
        IN_MODIFY
        | IN_CLOSE_WRITE
        | IN_CREATE
        | IN_DELETE
        | IN_MOVED_FROM
        | IN_MOVED_TO
        | IN_ATTRIB
    )

    def __init__(self, base_dir, on_change_callback, ignored_patterns):
        self.base_dir = Path(base_dir).resolve()
        self.on_change_callback = on_change_callback
        self.ignored_patterns = ignored_patterns
        self.libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.fd = -1
        self.wd_to_path = {}
        self.path_to_wd = {}
        self.running = False

    def start(self):
        self.fd = self.libc.inotify_init1(0x00080000)  # IN_CLOEXEC
        if self.fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"inotify_init1 failed: {os.strerror(errno)}")
        
        self.running = True
        self._add_watch_recursive(self.base_dir)
        log(f"[Watcher] Inotify aktif memantau {len(self.wd_to_path)} direktori.")

        thread = threading.Thread(target=self._read_loop, daemon=True)
        thread.start()

    def _add_watch_recursive(self, path):
        for root, dirs, _ in os.walk(path):
            root_path = Path(root)
            rel_path = root_path.relative_to(self.base_dir)
            if rel_path != Path(".") and is_ignored(rel_path, self.ignored_patterns):
                dirs[:] = []  # Don't recurse into ignored directories
                continue

            wd = self.libc.inotify_add_watch(self.fd, str(root_path).encode("utf-8"), self.WATCH_MASK)
            if wd >= 0:
                self.wd_to_path[wd] = root_path
                self.path_to_wd[root_path] = wd

    def _read_loop(self):
        buf_size = 4096
        fmt = "iIII"
        header_size = struct.calcsize(fmt)

        while self.running:
            try:
                data = os.read(self.fd, buf_size)
                if not data:
                    break
                
                offset = 0
                while offset + header_size <= len(data):
                    wd, mask, cookie, length = struct.unpack_from(fmt, data, offset)
                    name_bytes = data[offset + header_size : offset + header_size + length]
                    offset += header_size + length

                    name = name_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
                    dir_path = self.wd_to_path.get(wd)
                    if not dir_path:
                        continue

                    full_path = dir_path / name if name else dir_path
                    try:
                        rel_path = full_path.relative_to(self.base_dir)
                    except ValueError:
                        continue

                    if is_ignored(rel_path, self.ignored_patterns):
                        continue

                    # If new directory created, watch it
                    if (mask & self.IN_ISDIR) and (mask & (self.IN_CREATE | self.IN_MOVED_TO)):
                        if full_path.is_dir() and full_path not in self.path_to_wd:
                            new_wd = self.libc.inotify_add_watch(self.fd, str(full_path).encode("utf-8"), self.WATCH_MASK)
                            if new_wd >= 0:
                                self.wd_to_path[new_wd] = full_path
                                self.path_to_wd[full_path] = new_wd

                    self.on_change_callback(str(rel_path))
            except Exception as e:
                if self.running:
                    log(f"[Watcher] Inotify loop warning: {e}")
                    time.sleep(0.5)

    def stop(self):
        self.running = False
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except Exception:
                pass

# ==========================================
# Polling Fallback Watcher
# ==========================================
class PollingWatcher:
    def __init__(self, base_dir, on_change_callback, ignored_patterns, interval=2.0):
        self.base_dir = Path(base_dir).resolve()
        self.on_change_callback = on_change_callback
        self.ignored_patterns = ignored_patterns
        self.interval = interval
        self.file_mtimes = {}
        self.running = False

    def start(self):
        self.running = True
        self.file_mtimes = self._scan_files()
        log(f"[Watcher] Polling fallback aktif memantau {len(self.file_mtimes)} files.")
        thread = threading.Thread(target=self._poll_loop, daemon=True)
        thread.start()

    def _scan_files(self):
        mtimes = {}
        for root, dirs, files in os.walk(self.base_dir):
            root_path = Path(root)
            rel_root = root_path.relative_to(self.base_dir)
            if rel_root != Path(".") and is_ignored(rel_root, self.ignored_patterns):
                dirs[:] = []
                continue

            for file in files:
                file_path = root_path / file
                rel_file = file_path.relative_to(self.base_dir)
                if is_ignored(rel_file, self.ignored_patterns):
                    continue
                try:
                    mtimes[str(rel_file)] = file_path.stat().st_mtime
                except Exception:
                    pass
        return mtimes

    def _poll_loop(self):
        while self.running:
            time.sleep(self.interval)
            current_scan = self._scan_files()
            
            # Check modified / created
            for path_str, mtime in current_scan.items():
                if path_str not in self.file_mtimes or self.file_mtimes[path_str] != mtime:
                    self.on_change_callback(path_str)

            # Check deleted
            for path_str in self.file_mtimes:
                if path_str not in current_scan:
                    self.on_change_callback(path_str)

            self.file_mtimes = current_scan

    def stop(self):
        self.running = False

# ==========================================
# Main AutoBackup Engine
# ==========================================
class AutoBackupEngine:
    def __init__(self):
        self.config = load_config()
        self.base_dir = BASE_DIR
        self.git_handler = GitHandler(self.base_dir, self.config)
        self.snapshot_handler = SnapshotHandler(self.base_dir, self.config)
        self.pending_changes = set()
        self.last_event_time = 0
        self.lock = threading.Lock()
        self.running = False
        self.watcher = None
        self.debounce_sec = float(self.config.get("debounce_seconds", 3))

    def on_file_changed(self, rel_path):
        with self.lock:
            self.pending_changes.add(rel_path)
            self.last_event_time = time.time()
            log(f"[Event] File modified/changed: {rel_path}", to_file=False)

    def trigger_backup(self, specific_files=None):
        with self.lock:
            files_to_backup = list(specific_files if specific_files else self.pending_changes)
            self.pending_changes.clear()

        if not files_to_backup:
            # Check git status in case files were altered outside event catch
            try:
                st = self.git_handler._run_git("status", "--porcelain")
                if not st.stdout.strip():
                    log("[Backup] Tidak ada perubahan file untuk di-backup.")
                    return None
            except Exception:
                return None

        log(f"[Backup] Menjalankan auto-backup untuk {len(files_to_backup)} file...")
        
        mode = self.config.get("backup_mode", "both")
        commit_hash = None
        snap_id = None

        if mode in ("both", "git") and self.config.get("git_enabled", True):
            commit_hash = self.git_handler.commit_changes(files_to_backup)

        if mode in ("both", "snapshot") and self.config.get("snapshot_enabled", True):
            snap_id = self.snapshot_handler.create_snapshot(files_to_backup, commit_hash)

        summary = []
        if commit_hash:
            summary.append(f"Git commit: {commit_hash}")
        if snap_id:
            summary.append(f"Snapshot: {snap_id}")
        
        if summary:
            log(f"[Backup Selesai] {' | '.join(summary)}")
        else:
            log("[Backup] Tidak ada perubahan baru yang perlu disimpan.")

    def run_foreground(self):
        log("=== Memulai AutoBackup Service (Foreground Mode) ===")
        log(f"Workspace: {self.base_dir}")
        log(f"Backup Mode: {self.config.get('backup_mode')} | Debounce: {self.debounce_sec}s")
        
        self.running = True
        try:
            self.watcher = InotifyWatcher(self.base_dir, self.on_file_changed, self.config.get("ignored_patterns", []))
            self.watcher.start()
        except Exception as e:
            log(f"[Watcher] Inotify gagal ({e}). Beralih ke PollingWatcher...")
            self.watcher = PollingWatcher(self.base_dir, self.on_file_changed, self.config.get("ignored_patterns", []))
            self.watcher.start()

        try:
            while self.running:
                time.sleep(0.5)
                with self.lock:
                    has_pending = bool(self.pending_changes)
                    elapsed = time.time() - self.last_event_time
                
                if has_pending and elapsed >= self.debounce_sec:
                    self.trigger_backup()
        except KeyboardInterrupt:
            log("\n[Service] Dihentikan oleh user (Ctrl+C).")
        finally:
            if self.watcher:
                self.watcher.stop()
            log("=== AutoBackup Service Berhenti ===")

# ==========================================
# Daemon & CLI Control Commands
# ==========================================
def is_pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def get_daemon_pid():
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
                if is_pid_running(pid):
                    return pid
        except Exception:
            pass
    return None

def start_daemon():
    pid = get_daemon_pid()
    if pid:
        print(f"[AutoBackup] Service sudah berjalan di background (PID: {pid}).")
        return

    # Spawn background daemon process
    cmd = [sys.executable, str(Path(__file__).resolve()), "run"]
    process = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setpgrp
    )
    
    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))

    time.sleep(1)
    if is_pid_running(process.pid):
        print(f"✓ AutoBackup daemon berhasil dijalankan di background (PID: {process.pid})")
        print(f"  Log file : {LOG_FILE}")
        print(f"  Gunakan `python3 autobackup.py status` untuk memantau.")
    else:
        print(f"✗ Gagal menjalankan daemon. Periksa {LOG_FILE} untuk detail error.")

def stop_daemon():
    pid = get_daemon_pid()
    if not pid:
        print("[AutoBackup] Tidak ada service yang sedang berjalan.")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.3)
            if not is_pid_running(pid):
                break
        if is_pid_running(pid):
            os.kill(pid, signal.SIGKILL)
        print(f"✓ AutoBackup daemon (PID: {pid}) berhasil dihentikan.")
    except Exception as e:
        print(f"Error stopping daemon: {e}")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()

def set_remote_url(url):
    config = load_config()
    config["git_remote_url"] = url.strip()
    config["git_auto_push"] = True
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print(f"✓ Berhasil menyimpan remote GitHub ke autobackup.json")
    except Exception as e:
        print(f"✗ Gagal menulis konfigurasi: {e}")

    engine = AutoBackupEngine()
    engine.git_handler.set_remote(url.strip())
    print(f"✓ Git remote 'origin' berhasil diatur.")
    print("Mencoba push commit yang ada ke GitHub...")
    success = engine.git_handler.push()
    if success:
        print("✓ Sukses terhubung dan ter-push ke GitHub!")
    else:
        print("! Catatan: Jika push gagal karena autentikasi, pastikan Personal Access Token (PAT) atau SSH key valid.")

def trigger_push():
    engine = AutoBackupEngine()
    engine.git_handler.push()

def print_status():
    pid = get_daemon_pid()
    config = load_config()
    print("==================================================")
    print("               AUTOBACKUP STATUS                  ")
    print("==================================================")
    if pid:
        print(f"Status           : AKTIF (Running in Background)")
        print(f"PID              : {pid}")
    else:
        print(f"Status           : BERHENTI (Stopped)")
    
    print(f"Backup Mode      : {config.get('backup_mode')}")
    print(f"Debounce Delay   : {config.get('debounce_seconds')} detik")
    print(f"Snapshot Retensi : {config.get('max_snapshot_history')} max")
    
    remote_url = config.get("git_remote_url", "").strip()
    if remote_url:
        # Mask token if present
        display_url = remote_url
        if "@" in display_url and "://" in display_url:
            proto, rest = display_url.split("://", 1)
            creds, host_path = rest.split("@", 1)
            display_url = f"{proto}://***@{host_path}"
        print(f"GitHub Remote    : {display_url} (Auto-Push: {'ON' if config.get('git_auto_push') else 'OFF'})")
    else:
        print(f"GitHub Remote    : (Belum diatur - gunakan `python3 autobackup.py set-remote <url>`)")

    print(f"Log File         : {LOG_FILE}")

    # Show recent commits
    engine = AutoBackupEngine()
    commits = engine.git_handler.get_history(limit=5)
    print("\n--- Riwayat 5 Backup Terakhir (Git) ---")
    if commits:
        for c in commits:
            print(f"[{c['hash']}] {c['date']} - {c['message']}")
    else:
        print("(Belum ada riwayat backup git)")

    # Show recent snapshots
    snapshots = engine.snapshot_handler.list_snapshots(limit=5)
    print("\n--- Riwayat 5 Snapshot Terakhir (.backups/) ---")
    if snapshots:
        for s in snapshots:
            print(f"[{s['id']}] {s.get('timestamp', '-')} ({s.get('files_count', 0)} files)")
    else:
        print("(Belum ada riwayat snapshot)")
    print("==================================================")

def show_history(limit=20):
    engine = AutoBackupEngine()
    commits = engine.git_handler.get_history(limit=limit)
    print("==================================================")
    print(f"            RIWAYAT AUTO BACKUP ({len(commits)})            ")
    print("==================================================")
    if not commits:
        print("Belum ada riwayat backup.")
        return

    for idx, c in enumerate(commits, 1):
        print(f"{idx:2d}. [{c['hash']}] {c['date']}")
        print(f"    {c['message']}")
    print("==================================================")
    print("Tip: Gunakan `python3 autobackup.py diff <hash>` untuk melihat perubahan file.")
    print("     Gunakan `python3 autobackup.py restore <hash>` untuk mengembalikan workspace.")

def show_diff(commit_hash=None):
    engine = AutoBackupEngine()
    diff = engine.git_handler.get_diff(commit_hash)
    print(diff if diff else "Tidak ada perbedaan.")

def restore_backup(target_id):
    engine = AutoBackupEngine()
    print(f"Memproses restore ke target: {target_id}...")
    if target_id.startswith("snap_"):
        success = engine.snapshot_handler.restore_snapshot(target_id)
    else:
        success = engine.git_handler.rollback(target_id)

    if success:
        print(f"✓ Berhasil mengembalikan workspace ke {target_id}")
    else:
        print(f"✗ Gagal melakukan restore ke {target_id}")

def print_help():
    print("""
Penggunaan: python3 autobackup.py <perintah>

Perintah yang tersedia:
  start                Jalankan auto-backup daemon di background
  stop                 Hentikan auto-backup daemon
  restart              Restart auto-backup daemon
  status               Cek status proses dan riwayat backup
  run                  Jalankan auto-backup di foreground (live logs)
  set-remote <url>     Hubungkan repository GitHub (HTTPS Token / SSH)
  push                 Push manual commit ke GitHub
  history              Lihat riwayat lengkap backup & commit
  diff [hash]          Lihat detail perbedaan file pada commit tertentu
  restore <id>         Kembalikan file workspace ke commit hash atau snapshot ID
  backup-now           Jalankan backup manual langsung saat ini
  help                 Tampilkan panduan ini
""")

def main():
    if len(sys.argv) < 2:
        print_help()
        return

    cmd = sys.argv[1].lower()
    if cmd == "run":
        engine = AutoBackupEngine()
        engine.run_foreground()
    elif cmd == "start":
        start_daemon()
    elif cmd == "stop":
        stop_daemon()
    elif cmd == "restart":
        stop_daemon()
        time.sleep(1)
        start_daemon()
    elif cmd == "status":
        print_status()
    elif cmd == "set-remote":
        if len(sys.argv) < 3:
            print("Error: Harap masukkan URL GitHub repository. Contoh:")
            print("  python3 autobackup.py set-remote https://ghp_TOKEN@github.com/username/repo.git")
            print("  atau:")
            print("  python3 autobackup.py set-remote git@github.com:username/repo.git")
            return
        set_remote_url(sys.argv[2])
    elif cmd == "push":
        trigger_push()
    elif cmd == "history":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        show_history(limit)
    elif cmd == "diff":
        commit_hash = sys.argv[2] if len(sys.argv) > 2 else None
        show_diff(commit_hash)
    elif cmd == "restore":
        if len(sys.argv) < 3:
            print("Error: Harap sebutkan commit hash atau snapshot ID. Contoh: `python3 autobackup.py restore a1b2c3d`")
            return
        restore_backup(sys.argv[2])
    elif cmd == "backup-now":
        engine = AutoBackupEngine()
        engine.trigger_backup()
        print("✓ Backup manual selesai.")
    elif cmd in ("-h", "--help", "help"):
        print_help()
    else:
        print(f"Perintah tidak dikenal: {cmd}")
        print_help()

if __name__ == "__main__":
    main()

