# Radio Delay

Play an internet radio stream **delayed by any amount**, so a radio commentary can be lined up with the
TV picture (radio feeds and TV broadcasts are rarely in sync).

Windows desktop app, Python + tkinter, single window.

## Download

Grab `RadioDelay.exe` from the [Releases](../../releases/latest) page and double-click it. It is a single
portable file: no installation, no Python needed. Windows SmartScreen may warn because the exe is not
code-signed — choose **More info → Run anyway**.

Settings (`config.json`: last link, delay, volume, your playlist) are stored **next to the exe**, so the app stays
portable. If that folder is read-only it falls back to `%LOCALAPPDATA%\RadioDelay`.

## Run from source

Requires Python 3.9+.

```bat
Run Radio Delay.bat
```

The launcher creates `.venv`, installs the dependencies and starts the app. Manually:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python radio_delay.py
```

## Using it

1. Paste a **direct** stream address (usually ends in `.mp3`, `.aac`, `.m3u8` or `.pls`) — a station web
   page will not work — or pick a saved station from the playlist, then press **Play**.
2. Raise the delay with the slider or the −10/−5/−1/+1/+5/+10 s buttons until the commentary matches the
   picture. Changes apply instantly while playing, with no reconnect.
3. Use the **Volume** slider to match the radio's loudness to the TV; it applies while playing and is
   remembered.
4. Arrow keys fine-tune: ← → 0.5 s, ↑ ↓ 5 s.
5. `+ Add` saves the current link with a name; `Remove` deletes the selected station. Both are stored in
   `config.json`.

How it works: ffmpeg (bundled with the app) decodes the stream into raw PCM held in a 4-minute ring
buffer; playback reads from a point N seconds behind the newest audio. Nothing is re-encoded and the
buffer is never skipped or resampled, so the audio stays clean.

## Build the single-file exe

```bat
python -m pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name "Radio Delay" ^
    --collect-all imageio_ffmpeg --collect-all sounddevice radio_delay.py
```

The result is `dist\Radio Delay.exe` (about 60 MB: Python, tkinter, NumPy, PortAudio and ffmpeg are
inside it). Verify a build without clicking anything:

```bat
"dist\Radio Delay.exe" --selftest
```

Exit code `0` means ffmpeg was found, the audio device opened and roughly a second of the configured
station was decoded.

## Publishing a release

Do **not** commit the exe (GitHub warns above 50 MB and blocks above 100 MB); attach it to a release
instead.

1. Push this folder to GitHub (contents as the repository root).
2. On GitHub: **Releases → Draft a new release**.
3. **Choose a tag** → type `v1.0.0` → *Create new tag on publish*.
4. Title it, e.g. `Radio Delay 1.0`, describe the change.
5. Drag `Radio Delay.exe` into the *Attach binaries* box. Optionally rename the asset to
   `RadioDelay.exe` for a cleaner download URL.
6. **Publish release**.

The stable download link then is:

```
https://github.com/<user>/<repo>/releases/latest/download/RadioDelay.exe
```

With the GitHub CLI (`gh auth login` first):

```bat
gh release create v1.0.0 "Radio Delay.exe" --title "Radio Delay 1.0" --notes "First release"
```

## Notes

- Some stations push a burst of buffered audio when you connect; the app keeps it, so 0 s means "as live
  as this app can get", typically about a second behind the source.
- Delay is only ever added: if the radio is *behind* the TV, no delay setting will fix it.
- Audio quality is the station's: many sports streams are 48–128 kbps.

## License

MIT — see [LICENSE](LICENSE).
