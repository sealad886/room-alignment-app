from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from room_alignment.scanner import iter_scan_records


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg toolchain required")
class MediaCompatibilityMatrixTests(unittest.TestCase):
    def ffmpeg(self, *arguments: str) -> None:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_matrix_preserves_timing_geometry_color_and_audio_characteristics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.mp4"
            self.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=0.6", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=0.6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1", "-shortest", str(base))
            rotated = root / "rotated.mov"
            self.ffmpeg("-display_rotation:v:0", "90", "-i", str(base), "-c", "copy", str(rotated))
            sar = root / "sar.mp4"
            self.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=0.5", "-vf", "setsar=4/3", "-c:v", "libx264", "-an", str(sar))
            hdr = root / "hdr-signaled.mp4"
            self.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=0.5", "-c:v", "libx264", "-x264-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc", "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-an", str(hdr))
            vfr = root / "vfr.mkv"
            self.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=1", "-vf", "select=not(mod(n\\,3))", "-fps_mode", "vfr", "-c:v", "ffv1", "-an", str(vfr))
            malformed = root / "malformed.mp4"
            malformed.write_bytes(b"not a media container")
            truncated = root / "truncated.mp4"
            truncated.write_bytes(base.read_bytes()[: max(32, base.stat().st_size // 4)])
            unicode_directory = root / ("camera-α-" + "long-" * 20)
            unicode_directory.mkdir()
            shutil.copyfile(base, unicode_directory / "clip-雪.mp4")

            records = {item.relative_path: item for item in iter_scan_records(root, "matrix", probe_workers=4)}
            self.assertGreaterEqual(len(records), 8)
            base_record = records[base.name]
            base_audio = next(stream for stream in base_record.streams if stream["codecType"] == "audio")
            self.assertEqual(int(base_audio["sampleRate"]), 44_100)
            self.assertEqual(base_audio["channels"], 1)
            self.assertFalse(any(stream["codecType"] == "audio" for stream in records[sar.name].streams))
            sar_video = next(stream for stream in records[sar.name].streams if stream["codecType"] == "video")
            self.assertEqual(sar_video["sampleAspectRatio"], "4:3")
            hdr_video = next(stream for stream in records[hdr.name].streams if stream["codecType"] == "video")
            self.assertEqual(hdr_video["colorTransfer"], "smpte2084")
            rotated_video = next(stream for stream in records[rotated.name].streams if stream["codecType"] == "video")
            self.assertNotIn(rotated_video.get("rotation"), {None, 0, "0"})
            vfr_video = next(stream for stream in records[vfr.name].streams if stream["codecType"] == "video")
            self.assertIn("timeBase", vfr_video)
            self.assertIn("averageFrameRate", vfr_video)
            self.assertTrue(records[malformed.name].warning)
            self.assertTrue(records[truncated.name].warning)
            unicode_record = next(item for path, item in records.items() if "雪" in path)
            self.assertIsNone(unicode_record.warning)


if __name__ == "__main__":
    unittest.main()
