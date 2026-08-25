import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from room_alignment.models import MediaRecord, ScanSummary
from room_alignment.render import build_ffmpeg_command
from room_alignment.scanner import probe
from room_alignment.store import Store


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RenderIntegrationTests(unittest.TestCase):
    def test_normalizes_mixed_geometry_and_renders_independent_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "library"; root.mkdir()
            specs = [("a.mp4", "320x180", "440"), ("b.mp4", "640x360", "660")]
            records = []
            for index, (name, size, frequency) in enumerate(specs):
                path = root / name
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x223344:s={size}:r=24:d=1",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
                ], check=True)
                values, _, warning = probe(path)
                self.assertIsNone(warning)
                records.append(MediaRecord(f"m{index}", "lib", name, path.stat().st_size, path.stat().st_mtime_ns, camera=f"Cam {index}", **values))
            store = Store(Path(directory) / "state.sqlite3")
            store.save_scan(ScanSummary("lib", str(root), 2, 2, 0, ["Cam 0", "Cam 1"], {}), records)
            project = {
                "id":"p","name":"Mixed","libraryId":"lib",
                "videoSegments":[
                    {"id":"V-1","mediaId":"m0","start":0,"end":0.5,"sourceIn":0},
                    {"id":"V-2","mediaId":"m1","start":0.5,"end":1,"sourceIn":0},
                ],
                "audioSegments":[{"id":"A-1","mediaId":"m1","start":0,"end":1,"sourceIn":0,"linked":False}],
            }
            output = Path(directory) / "output.mp4"
            command = build_ffmpeg_command(store, project, output)
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            result, _, warning = probe(output)
            self.assertIsNone(warning)
            self.assertEqual((result["width"], result["height"]), (320, 180))
            self.assertEqual(result["audio_codec"], "aac")
            self.assertAlmostEqual(result["duration"], 1, delta=0.08)


if __name__ == "__main__":
    unittest.main()
