import os
import sys
import unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import media_ai
import media_transport

# A minimal RIFF/WAVE header: enough for media_transport.sniff to accept it.
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


class MediaAiTests(unittest.TestCase):
    # Stage statuses were localised in media_pipeline.stage_config: an unset
    # *_ENABLED switch reports 未启用 (enabled but unconfigured: 未配置).
    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SANAE_AUDIO_TRANSCRIPTION_ENABLED", None)
            text, status = media_ai.transcribe({"data": b"x", "contentType": "audio/wav"})
        self.assertIsNone(text)
        self.assertIn("未启用", status)

    def test_explicit_media_is_downloaded_once(self):
        event = {"message": [{"type": "record", "data": {"url": "https://example.invalid/a"}}]}
        # fetch_segments -> media_transport.fetch_media, which calls its own
        # module-level download(); the media_ai re-export is not on that path.
        with mock.patch.object(media_transport, "download", return_value=(WAV_BYTES, "audio/wav")) as download:
            items = media_ai.fetch_segments(event, "http://127.0.0.1:3002")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get("format"), "wav")
        download.assert_called_once()

    def test_image_host_does_not_upload_by_default(self):
        os.environ.pop("SANAE_IMAGE_HOST_ENABLED", None)
        result, status = media_ai.image_host_upload(b"x")
        self.assertIsNone(result)
        self.assertIn("disabled", status)

    def test_video_is_disabled_by_default(self):
        os.environ.pop("SANAE_VIDEO_UNDERSTANDING_ENABLED", None)
        result, status = media_ai.understand_video({"data": b"x", "contentType": "video/mp4"})
        self.assertIsNone(result)
        self.assertIn("未启用", status)


if __name__ == "__main__":
    unittest.main()
