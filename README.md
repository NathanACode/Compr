# Compr

A small Windows GUI that turns a full football broadcast plus a list of event
timecodes into either a folder of short event clips, a single compiled video,
or both.

## Features

- Accepts `.mp4`, `.mkv`, `.mov` source video.
- Parses raw event paste of the form `H1 8'49" · PassH1 9'05" · Progressive Pass…`
  (no separators required).
- Converts match-clock times into video offsets using user-supplied first- and
  second-half kickoffs.
- Configurable clip length (default 20s, centered on the event).
- Greedy multi-event merging: when consecutive events fall within one clip
  length of each other (same half), they are merged into a single longer clip
  with `[L/2 padding, ev1, gaps, …, evN, L/2 padding]`. Filenames reflect the
  merge, e.g. `02-04_Pass+Pass+Progressive_Pass_H1_8'49-9'05.mp4`.
- Output as individual files, a single compiled MP4, or both at once.
- Optional 0.5s fade-to-black between clips on the compiled output (does not
  affect the individual files).
- Optional "include audio" toggle.
- Live progress bar with rolling-average ETA.
- Dark theme with green / purple / white accents.

## Requirements

- Windows.
- A copy of `ffmpeg.exe` placed next to `compr.py` (or next to `Compr.exe`
  for the bundled build). Not committed to this repo.

## Run from source

```
python compr.py
```

## Build a standalone exe

```
pyinstaller --onefile --windowed --name Compr compr.py
```

The result is `dist/Compr.exe`. Place `ffmpeg.exe` alongside it before running.

## Notes

- A macOS port is planned. All theme colours/fonts are kept in a single
  `THEME` dict at the top of `compr.py` to keep the port mechanical.
