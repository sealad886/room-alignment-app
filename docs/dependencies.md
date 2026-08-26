# Runtime and dependency declaration

The installed application has no third-party Python or browser package dependency. Python standard-library APIs used by the service are documented at [Python subprocess](https://docs.python.org/3/library/subprocess.html), [sqlite3](https://docs.python.org/3/library/sqlite3.html), and [http.server](https://docs.python.org/3/library/http.server.html). SQLite transaction/backup behavior is described by the [upstream backup API](https://www.sqlite.org/backup.html).

Hatchling 1.27.0 is an exact build-only dependency declared in `pyproject.toml`. It is resolved inside an isolated PEP 517 build environment and is not installed with or imported by Room Alignment at runtime. Wheel verification installs with `--no-index --no-deps` to prove this boundary.

FFmpeg/FFprobe are external maintained runtime tools, invoked through documented structured command arguments; see the [upstream FFmpeg tool documentation](https://ffmpeg.org/ffmpeg.html). A render plan and completed manifest record the exact first version line observed from each executable. `room-alignment doctor` reports availability and bounded first-line versions without absolute paths, and fails readiness when the major version is missing or below 6. The validated local release-candidate matrix is:

| Runtime | Supported floor | Release-candidate observation |
|---|---:|---:|
| Python | 3.11 | 3.14.7 |
| FFmpeg | 6.0 | 9.0.1 |
| FFprobe | 6.0 | 9.0.1 |

No application code downloads or upgrades these tools. Operators must install them through an OS/project-managed, reproducible workflow and re-run the full media matrix when changing the major version. The toolchain observation is compatibility evidence, not a claim that all FFmpeg builds share codecs: preflight and render tests must run against the actual build.

Python is PSF-licensed. SQLite is public domain. FFmpeg licensing depends on build configuration (LGPL/GPL and optional component licenses); distribution or packaging must inspect the actual build with `ffmpeg -L` and satisfy its terms. The wheel/source archive does not redistribute FFmpeg.
