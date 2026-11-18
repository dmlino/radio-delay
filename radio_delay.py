#!/usr/bin/env python3
"""Radio Delay - time-shift an internet radio stream so it matches the TV picture.

A bundled ffmpeg decodes the stream into raw PCM, which is kept in a ring buffer.
Playback reads from a point N seconds behind the live edge, so the audio can be
delayed by any amount while it keeps playing (a live time-shift buffer).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np
import sounddevice as sd

def _app_dir() -> Path:
    """Folder the app lives in: beside the .py, or beside the .exe when packaged."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _config_path(base: Path) -> Path:
    """Settings travel with the app so it stays portable; fall back if the folder is read-only."""
    if os.access(base, os.W_OK):
        return base / "config.json"
    fallback = Path(os.environ.get("LOCALAPPDATA") or base) / "RadioDelay"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "config.json"
    except OSError:
        return base / "config.json"


APP_DIR = _app_dir()
CONFIG_PATH = _config_path(APP_DIR)


def _find_ffmpeg() -> str:
    """Locate an ffmpeg binary: the pip-installed one first, then whatever is on PATH."""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    for name in ("ffmpeg.exe", "ffmpeg"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "ffmpeg is missing. Close this window and start the app with 'Run Radio Delay.bat' "
        "once so it can install its dependencies."
    )


class DelayPlayer:
    """Decode a live stream to PCM, then play it from an adjustable offset behind live."""

    BLOCK = 1024          # audio callback size in frames (~23 ms at 44.1 kHz)
    BASE_LATENCY = 1.0    # seconds of slack always kept between live edge and playback
    CHANNELS = 2
    MAX_DELAY = 180.0     # longest delay the ring buffer supports
    BUFFER_SECONDS = 240

    def __init__(self, notify):
        self._notify = notify
        self._ffmpeg = _find_ffmpeg()
        self._lock = threading.Lock()
        self._rate = 44100
        self._delay = 0.0
        self._delay_version = 0
        self._applied_version = 0
        self._buffer = np.zeros((self._rate * self.BUFFER_SECONDS, self.CHANNELS), dtype=np.int16)
        self._write_total = 0
        self._read_pos = 0.0
        self._primed = False
        self._pending = b""
        self._stream = None
        self._proc = None
        self._worker = None
        self._stop_event = threading.Event()
        self._stderr: deque[str] = deque(maxlen=8)
        self._playing = False
        self._volume = 1.0

    # ------------------------------------------------------------------ state

    @property
    def playing(self) -> bool:
        return self._playing and not self._stop_event.is_set()

    @property
    def delay(self) -> float:
        return self._delay

    def set_delay(self, seconds: float) -> float:
        seconds = max(0.0, min(self.MAX_DELAY, round(float(seconds) * 2) / 2))
        with self._lock:
            if seconds != self._delay:
                self._delay = seconds
                self._delay_version += 1
        return seconds

    @property
    def volume(self) -> float:
        with self._lock:
            return self._volume

    def set_volume(self, value: float) -> float:
        value = max(0.0, min(1.0, round(float(value), 2)))
        with self._lock:
            self._volume = value
        return value

    # ------------------------------------------------------------- transport

    def start(self, url: str) -> bool:
        self.stop()
        url = url.strip()
        if not url:
            self._notify("Paste a stream link first.", "error")
            return False

        try:
            rate = int(sd.query_devices(kind="output")["default_samplerate"]) or 44100
        except Exception:
            rate = 44100
        self._rate = rate
        with self._lock:
            self._buffer = np.zeros((rate * self.BUFFER_SECONDS, self.CHANNELS), dtype=np.int16)
            self._write_total = 0
            self._read_pos = 0.0
            self._primed = False
        self._pending = b""
        self._stderr.clear()
        self._stop_event = threading.Event()

        try:
            self._stream = sd.OutputStream(
                samplerate=rate,
                channels=self.CHANNELS,
                dtype="int16",
                blocksize=self.BLOCK,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._notify(f"No audio output device available: {exc}", "error")
            return False

        self._playing = True
        self._notify("Connecting to stream ...", "busy")
        self._worker = threading.Thread(target=self._pump, args=(url,), daemon=True)
        self._worker.start()
        return True

    def stop(self) -> None:
        self._playing = False
        self._stop_event.set()
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        worker, self._worker = self._worker, None
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._primed = False

    # ------------------------------------------------------------- ingestion

    def _pump(self, url: str) -> None:
        """Keep an ffmpeg decoder running and feed its PCM into the ring buffer."""
        failures = 0
        while not self._stop_event.is_set():
            proc = self._spawn(url)
            if proc is None:
                return
            self._proc = proc
            received = 0
            announced = False
            try:
                fd = proc.stdout.fileno()
                while not self._stop_event.is_set():
                    data = os.read(fd, 8192)
                    if not data:
                        break
                    received += len(data)
                    if not announced:
                        announced = True
                        self._notify("Playing", "ok")
                    self._push(data)
            except (OSError, ValueError):
                pass
            finally:
                self._kill(proc)
            if self._stop_event.is_set():
                break

            failures += 1
            if received == 0:
                if failures >= 2:
                    detail = self._stderr[-1] if self._stderr else f"ffmpeg exit code {proc.returncode}"
                    self._notify(f"Could not open this stream. {detail}", "error")
                    self._playing = False
                    return
                self._notify("Could not open this stream, retrying ...", "busy")
                self._stop_event.wait(2.0)
                continue
            if failures > 20:
                self._notify("The stream keeps dropping. Press Play to try again.", "error")
                self._playing = False
                return
            self._notify(f"Stream dropped, reconnecting ({failures}) ...", "busy")
            self._stop_event.wait(2.0)

    def _spawn(self, url: str) -> subprocess.Popen | None:
        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel", "error",
            "-rw_timeout", "15000000",
            "-i", url,
            "-vn",
            "-ac", str(self.CHANNELS),
            "-ar", str(self._rate),
            "-f", "s16le",
            "pipe:1",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._notify(f"Could not start ffmpeg: {exc}", "error")
            return None
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        return proc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr.append(line)
        except (OSError, ValueError):
            pass

    def _kill(self, proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def _push(self, data: bytes) -> None:
        if self._pending:
            data = self._pending + data
        frame_bytes = 2 * self.CHANNELS
        usable = len(data) - (len(data) % frame_bytes)
        self._pending = data[usable:]
        if usable == 0:
            return
        frames = np.frombuffer(data[:usable], dtype="<i2").reshape(-1, self.CHANNELS)
        n = len(frames)
        with self._lock:
            cap = len(self._buffer)
            if n >= cap:
                frames = frames[-cap:]
                n = cap
            end = self._write_total % cap
            first = min(n, cap - end)
            self._buffer[end:end + first] = frames[:first]
            if n > first:
                self._buffer[:n - first] = frames[first:]
            self._write_total += n

    # -------------------------------------------------------------- playback

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ARG002
        try:
            self._fill(outdata, frames)
        except Exception as exc:  # keep PortAudio alive no matter what
            outdata.fill(0)
            self._notify(f"Audio engine error: {exc}", "error")

    def _fill(self, outdata, frames: int) -> None:
        rate = self._rate
        cap = len(self._buffer)
        with self._lock:
            write_total = self._write_total
            offset = int((self.BASE_LATENCY + self._delay) * rate)

            if not self._primed:
                if write_total < offset + frames:
                    outdata.fill(0)
                    return
                self._read_pos = float(write_total - offset)
                self._applied_version = self._delay_version
                self._primed = True
            elif self._delay_version != self._applied_version:
                # The user changed the delay: shift by exactly that amount and keep
                # playing from the new point (older audio repeats, newer is skipped).
                self._read_pos = float(write_total - offset)
                self._applied_version = self._delay_version

            # Playback free-runs sample-exactly from here: never skip or repeat
            # samples to "correct" the offset, that is audible as roughness.
            target = write_total - offset
            available = int(write_total - self._read_pos)
            if available < frames * 2 or available > cap - frames * 4:
                # About to run dry, or the buffer is nearly full: re-anchor at the
                # requested offset instead of glitching or overwriting unread audio.
                self._read_pos = float(max(target, write_total - cap + frames))

            available = int(write_total - self._read_pos)
            if available <= 0:
                outdata.fill(0)
                return

            start = int(self._read_pos) % cap
            n = min(frames, available, cap)
            first = min(n, cap - start)
            outdata[:first] = self._buffer[start:start + first]
            if n > first:
                outdata[first:n] = self._buffer[:n - first]
            if n < frames:
                outdata[n:].fill(0)
            if self._volume != 1.0:
                # Simple sample scaling; at 100% the audio stays untouched.
                scaled = outdata.astype(np.float32)
                scaled *= self._volume
                outdata[:] = scaled.astype(np.int16)
            self._read_pos += frames


class RadioDelayApp:
    PRESET_STEPS = (-10, -5, -1, 1, 5, 10)
    STATUS_COLORS = {"error": "#b42318", "busy": "#8a5a00", "ok": "#1c6b3c"}
    DEFAULT_FAVORITES = [
        {"name": "Bola Branca", "url": "https://25683.live.streamtheworld.com/BOLA_BRANCAAAC.aac"},
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.status_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        root.title("Radio Delay")
        root.minsize(880, 450)

        self.url = tk.StringVar()
        self.delay_var = tk.DoubleVar(value=0.0)
        self.delay_text = tk.StringVar(value="0.0 s")
        self.status_text = tk.StringVar(value="Idle - pick a station or paste a stream link, then press Play.")
        self.volume_var = tk.DoubleVar(value=100.0)
        self.volume_text = tk.StringVar(value="100%")
        self.favorites: list[dict[str, str]] = []
        self._poll_id: str | None = None

        self.player = DelayPlayer(self._notify_from_engine)
        self._build()
        self._load_config()
        self._poll()

    # -------------------------------------------------------------------- UI

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text="Radio Delay", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            left,
            text="Delay a live radio stream until its commentary lines up with the TV picture.",
            font=("Segoe UI", 9),
        ).grid(row=1, column=0, sticky="w", pady=(2, 16))

        link = ttk.Frame(left)
        link.grid(row=2, column=0, sticky="ew")
        link.columnconfigure(1, weight=1)
        ttk.Label(link, text="Stream link").grid(row=0, column=0, padx=(0, 8))
        self.entry = ttk.Entry(link, textvariable=self.url, font=("Segoe UI", 10))
        self.entry.grid(row=0, column=1, sticky="ew")
        self.entry.bind("<Return>", lambda _event: self._toggle())
        self.play_btn = ttk.Button(link, text="Play", width=10, command=self._toggle)
        self.play_btn.grid(row=0, column=2, padx=(8, 0))

        self.status = ttk.Label(left, textvariable=self.status_text, font=("Segoe UI", 9), foreground="#4a4a4a")
        self.status.grid(row=3, column=0, sticky="w", pady=(10, 16))

        box = ttk.LabelFrame(left, text=" Delay ", padding=14)
        box.grid(row=4, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)

        ttk.Label(box, textvariable=self.delay_text, font=("Segoe UI", 34, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(box, text="seconds behind the live stream", font=("Segoe UI", 9)).grid(
            row=1, column=0, sticky="w", pady=(0, 12)
        )

        self.scale = ttk.Scale(
            box,
            from_=0.0,
            to=float(DelayPlayer.MAX_DELAY),
            variable=self.delay_var,
            command=self._on_scale,
        )
        self.scale.grid(row=2, column=0, sticky="ew")

        presets = ttk.Frame(box)
        presets.grid(row=3, column=0, sticky="w", pady=(12, 0))
        for col, step in enumerate(self.PRESET_STEPS):
            text = f"{step:+d} s".replace("-", "\u2212")
            ttk.Button(presets, text=text, width=6, command=lambda s=step: self._nudge(s)).grid(
                row=0, column=col, padx=(0, 6)
            )
        ttk.Button(presets, text="Live (0 s)", command=lambda: self._set_delay(0)).grid(
            row=0, column=len(self.PRESET_STEPS), padx=(10, 0)
        )

        volume = ttk.Frame(left)
        volume.grid(row=5, column=0, sticky="ew", pady=(16, 0))
        volume.columnconfigure(1, weight=1)
        ttk.Label(volume, text="Volume").grid(row=0, column=0, padx=(0, 8))
        self.volume_scale = ttk.Scale(
            volume,
            from_=0.0,
            to=100.0,
            variable=self.volume_var,
            command=self._on_volume,
        )
        self.volume_scale.grid(row=0, column=1, sticky="ew")
        ttk.Label(volume, textvariable=self.volume_text, width=5, anchor="e").grid(row=0, column=2, padx=(8, 0))

        ttk.Label(
            left,
            font=("Segoe UI", 9),
            foreground="#666666",
            wraplength=530,
            justify="left",
            text=(
                "Paste the direct stream address (usually ends in .mp3, .aac, .m3u8 or .pls), or pick a station "
                "from the playlist. A larger delay means the radio plays further behind. "
                "Arrow keys fine-tune: \u2190 \u2192 by 0.5 s, \u2191 \u2193 by 5 s."
            ),
        ).grid(row=6, column=0, sticky="w", pady=(14, 0))

        playlist = ttk.LabelFrame(outer, text=" Playlist ", padding=10)
        playlist.grid(row=0, column=1, sticky="nsw", padx=(16, 0))
        playlist.rowconfigure(0, weight=1)
        self.fav_list = tk.Listbox(
            playlist,
            width=30,
            height=14,
            font=("Segoe UI", 10),
            activestyle="none",
            exportselection=False,
            highlightthickness=0,
        )
        self.fav_list.grid(row=0, column=0, sticky="ns")
        scrollbar = ttk.Scrollbar(playlist, orient="vertical", command=self.fav_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.fav_list.configure(yscrollcommand=scrollbar.set)
        self.fav_list.bind("<Double-Button-1>", self._play_selected)
        self.fav_list.bind("<Return>", self._play_selected)

        buttons = ttk.Frame(playlist)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="Play", width=6, command=self._play_selected).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(buttons, text="+ Add", width=6, command=self._add_favorite).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(buttons, text="Edit", width=6, command=self._edit_favorite).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(buttons, text="Remove", width=8, command=self._remove_favorite).grid(row=0, column=3)

        for key, step in (("<Left>", -0.5), ("<Right>", 0.5), ("<Up>", 5.0), ("<Down>", -5.0)):
            self.root.bind(key, lambda _event, s=step: self._key_nudge(s))

    def _notify_from_engine(self, text: str, kind: str = "info") -> None:
        self.status_queue.put((text, kind))

    def _poll(self) -> None:
        try:
            while True:
                text, kind = self.status_queue.get_nowait()
                self._set_status(text, kind)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(150, self._poll)

    def _set_status(self, text: str, kind: str = "info") -> None:
        self.status_text.set(text)
        self.status.configure(foreground=self.STATUS_COLORS.get(kind, "#4a4a4a"))

    # --------------------------------------------------------------- actions

    def _on_scale(self, value: str) -> None:
        self._set_delay(float(value), from_scale=True)

    def _set_delay(self, seconds: float, from_scale: bool = False) -> None:
        seconds = self.player.set_delay(seconds)
        if not from_scale:
            self.delay_var.set(seconds)
        self.delay_text.set(f"{seconds:.1f} s")

    def _nudge(self, step: float) -> None:
        self._set_delay(self.player.delay + step)

    def _on_volume(self, value: str) -> None:
        self._set_volume(float(value), from_scale=True)

    def _set_volume(self, percent: float, from_scale: bool = False) -> None:
        percent = max(0.0, min(100.0, round(float(percent))))
        self.player.set_volume(percent / 100.0)
        if not from_scale:
            self.volume_var.set(percent)
        self.volume_text.set(f"{percent:.0f}%")

    def _key_nudge(self, step: float) -> None:
        if isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry, tk.Listbox)):
            return
        self._nudge(step)

    def _toggle(self) -> None:
        if self.player.playing:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        url = self.url.get().strip()
        if not url:
            self._set_status("Paste a stream link first.", "error")
            self.entry.focus_set()
            return
        if self.player.start(url):
            self.play_btn.configure(text="Stop")
            self._save_config()

    def _stop(self) -> None:
        self.player.stop()
        self.play_btn.configure(text="Play")
        self._set_status("Stopped.")

    # ---------------------------------------------------------------- config

    def _load_config(self) -> None:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if not isinstance(data, dict):
            data = {}

        stored = data.get("favorites")
        if isinstance(stored, list):
            self.favorites = [
                {
                    "name": str(item.get("name", "")).strip() or str(item.get("url", "")).strip(),
                    "url": str(item.get("url", "")).strip(),
                }
                for item in stored
                if isinstance(item, dict) and str(item.get("url", "")).strip()
            ]
        else:
            self.favorites = [dict(fav) for fav in self.DEFAULT_FAVORITES]
        self._refresh_favorites()

        url = str(data.get("url", "")).strip()
        if not url and self.favorites:
            url = self.favorites[0]["url"]
        self.url.set(url)
        try:
            self._set_delay(float(data.get("delay", 0)))
        except (TypeError, ValueError):
            pass
        try:
            self._set_volume(float(data.get("volume", 100)))
        except (TypeError, ValueError):
            pass

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(
                    {
                        "url": self.url.get().strip(),
                        "delay": self.player.delay,
                        "volume": round(self.player.volume * 100),
                        "favorites": self.favorites,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ------------------------------------------------------------- playlist

    def _refresh_favorites(self) -> None:
        self.fav_list.delete(0, "end")
        for fav in self.favorites:
            self.fav_list.insert("end", fav["name"])

    def _selected_favorite(self) -> dict[str, str] | None:
        selection = self.fav_list.curselection()
        if not selection:
            return None
        index = int(selection[0])
        if 0 <= index < len(self.favorites):
            return self.favorites[index]
        return None

    def _play_selected(self, _event=None) -> None:
        fav = self._selected_favorite()
        if fav is None:
            self._set_status("Select a station in the playlist first.", "error")
            return
        self.url.set(fav["url"])
        if self.player.playing:
            self._stop()
        self._start()

    def _ask_station(self, title: str, name: str = "", url: str = "") -> tuple[str, str] | None:
        """Modal form for one station: returns (name, url), or None when cancelled."""
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)

        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        name_var = tk.StringVar(value=name)
        url_var = tk.StringVar(value=url)

        ttk.Label(body, text="Station name").grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(body, textvariable=name_var, width=46, font=("Segoe UI", 10))
        name_entry.grid(row=1, column=0, sticky="ew", pady=(2, 12))

        ttk.Label(body, text="Stream link").grid(row=2, column=0, sticky="w")
        url_entry = ttk.Entry(body, textvariable=url_var, width=46, font=("Segoe UI", 10))
        url_entry.grid(row=3, column=0, sticky="ew", pady=(2, 14))

        answer: list[tuple[str, str]] = []

        def confirm(_event=None) -> None:
            answer.append((name_var.get().strip(), url_var.get().strip()))
            dialog.destroy()

        def cancel(_event=None) -> None:
            dialog.destroy()

        actions = ttk.Frame(body)
        actions.grid(row=4, column=0, sticky="e")
        ttk.Button(actions, text="Cancel", command=cancel).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(actions, text="Save", command=confirm).grid(row=0, column=1)

        dialog.bind("<Return>", confirm)
        dialog.bind("<Escape>", cancel)
        dialog.protocol("WM_DELETE_WINDOW", cancel)

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        name_entry.focus_set()

        dialog.grab_set()
        self.root.wait_window(dialog)
        return answer[0] if answer else None

    def _select_index(self, index: int) -> None:
        self.fav_list.selection_clear(0, "end")
        if 0 <= index < self.fav_list.size():
            self.fav_list.selection_set(index)
            self.fav_list.see(index)

    def _add_favorite(self) -> None:
        answer = self._ask_station("Add a station", "", self.url.get().strip())
        if answer is None:
            return
        name, url = answer
        if not url:
            self._set_status("A station needs a stream link.", "error")
            return
        name = name or self._suggest_name(url)
        existing = next((fav for fav in self.favorites if fav["url"] == url), None)
        if existing is not None:
            existing["name"] = name
            self._refresh_favorites()
            self._select_index(self.favorites.index(existing))
            self._save_config()
            self._set_status(f"That link was already saved - renamed it to '{name}'.", "ok")
            return
        self.favorites.append({"name": name, "url": url})
        self._refresh_favorites()
        self._select_index(len(self.favorites) - 1)
        self._save_config()
        self._set_status(f"Added '{name}' to the playlist.", "ok")

    def _edit_favorite(self) -> None:
        fav = self._selected_favorite()
        if fav is None:
            self._set_status("Select a station to edit it.", "error")
            return
        answer = self._ask_station("Edit station", fav["name"], fav["url"])
        if answer is None:
            return
        name, url = answer
        if not url:
            self._set_status("A station needs a stream link.", "error")
            return
        name = name or self._suggest_name(url)
        clash = next((item for item in self.favorites if item is not fav and item["url"] == url), None)
        if clash is not None:
            self._set_status(f"'{clash['name']}' already uses that link.", "error")
            return
        index = self.favorites.index(fav)
        was_current = self.url.get().strip() == fav["url"]
        fav["name"] = name
        fav["url"] = url
        if was_current:
            self.url.set(url)
        self._refresh_favorites()
        self._select_index(index)
        self._save_config()
        self._set_status(f"Updated '{name}'.", "ok")

    def _remove_favorite(self) -> None:
        fav = self._selected_favorite()
        if fav is None:
            self._set_status("Select a station in the playlist to remove it.", "error")
            return
        self.favorites = [item for item in self.favorites if item is not fav]
        self._refresh_favorites()
        self._save_config()
        self._set_status(f"Removed '{fav['name']}' from the playlist.", "ok")

    @staticmethod
    def _suggest_name(url: str) -> str:
        host = url.split("//")[-1].split("/")[0].split(":")[0].strip()
        return host or url

    def close(self) -> None:
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self._save_config()
        self.player.stop()
        self.root.destroy()


def run_selftest() -> int:
    """Headless check for the packaged build: ffmpeg found, audio device opened, stream decoded."""
    url = ""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        url = str(data.get("url", "")).strip()
    except Exception:
        pass
    if not url:
        url = RadioDelayApp.DEFAULT_FAVORITES[0]["url"]
    try:
        player = DelayPlayer(lambda *_args: None)
    except RuntimeError:
        return 2
    if not player.start(url):
        return 3
    deadline = time.monotonic() + 20
    decoded = 0
    while time.monotonic() < deadline:
        decoded = player._write_total
        if decoded > player._rate:
            break
        time.sleep(0.2)
    player.stop()
    return 0 if decoded > player._rate else 4


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        app = RadioDelayApp(root)
    except RuntimeError as exc:
        root.withdraw()
        messagebox.showerror("Radio Delay", str(exc))
        root.destroy()
        return
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()


if __name__ == "__main__":
    main()
