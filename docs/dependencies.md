# Runtime and dependency declaration

The application has no third-party Python or browser package dependency. Python standard-library APIs used by the service are documented at [Python subprocess](https://docs.python.org/3/library/subprocess.html), [sqlite3](https://docs.python.org/3/library/sqlite3.html), and [http.server](https://docs.python.org/3/library/http.server.html). SQLite transaction/backup behavior is described by the [upstream backup API](https://www.sqlite.org/backup.html).

FFmpeg/FFprobe are external maintained runtime tools, invoked through documented structured command arguments; see the [upstream FFmpeg tool documentation](https://ffmpeg.org/ffmpeg.html). A render plan and completed manifest record the exact first version line observed from each executable. The validated local release-candidate matrix is:

| Runtime | Supported floor | Release-candidate observation |
|---|---:|---:|
| Python | 3.11 | 3.14.7 |
| FFmpeg | 6.0 | 9.0.1 |
| FFprobe | 6.0 | 9.0.1 |

No application code downloads or upgrades these tools. Operators must install them through an OS/project-managed, reproducible workflow and re-run the full media matrix when changing the major version. The toolchain observation is compatibility evidence, not a claim that all FFmpeg builds share codecs: preflight and render tests must run against the actual build.

Python is PSF-licensed. SQLite is public domain. FFmpeg licensing depends on build configuration (LGPL/GPL and optional component licenses); distribution or packaging must inspect the actual build with `ffmpeg -L` and satisfy its terms. This source-only delivery does not redistribute FFmpeg.
