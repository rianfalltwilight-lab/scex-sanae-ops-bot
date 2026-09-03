import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from media_segments import media_segments, media_summary, parse_segments


class MediaSegmentTests(unittest.TestCase):
    def test_structured_segments_are_preferred(self):
        event = {"raw_message": "ignored", "message": [
            {"type": "record", "data": {"file": "voice.silk", "url": "https://x/voice"}},
            {"type": "text", "data": {"text": "hello"}},
        ]}
        self.assertEqual(media_segments(event)[0]["type"], "record")
        self.assertEqual(media_summary(event)[0]["name"], "voice")

    def test_cq_segments_and_dedup(self):
        raw = "[CQ:image,url=https://x/a.png][CQ:image,url=https://x/a.png][CQ:reply,id=1]"
        self.assertEqual(len(media_segments(raw)), 2)
        self.assertEqual(parse_segments(raw)[-1]["type"], "reply")


if __name__ == "__main__":
    unittest.main()
