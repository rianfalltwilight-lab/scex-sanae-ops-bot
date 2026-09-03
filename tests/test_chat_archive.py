import tempfile
import unittest
from datetime import datetime
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from chat_archive import ChatArchive, safe_group_name


class ChatArchiveTests(unittest.TestCase):
    def test_group_name_is_safe_and_has_fallback(self):
        self.assertEqual(safe_group_name('../群/名', 123), '群_名')
        self.assertEqual(safe_group_name('', 123), 'group-123')

    def test_daily_per_group_log_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = ChatArchive(Path(tmp))
            when = datetime(2026, 8, 21, 7, 1, 2)
            path = archive.append(1, '测试群', '狗蛋', '你好', when)
            archive.append(1, '测试群', '小明', '服务器怎么样', when)
            archive.append(2, '另一个群', '狗蛋', '隔离', when)
            self.assertEqual(path, Path(tmp) / '测试群' / '2026-08-21.log')
            self.assertEqual(archive.read(1, '测试群', '2026-08-21', player='狗蛋'),
                             ['07:01:02 狗蛋：你好'])
            self.assertEqual(archive.read(2, '另一个群', '2026-08-21'),
                             ['07:01:02 狗蛋：隔离'])


if __name__ == '__main__':
    unittest.main()
