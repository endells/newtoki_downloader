from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from newtoki_downloader.storage import browse_folders, check_storage
from newtoki_downloader.parser import SiteError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'NAS'
        self.root.mkdir()

    def test_folder_picker_excludes_external_symlinks_and_files(self):
        (self.root / '도서').mkdir()
        (self.root / 'existing.txt').write_text('keep')
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.root / 'escape').symlink_to(outside, target_is_directory=True)
        result = browse_folders(str(self.root), [self.root])
        self.assertEqual([d['name'] for d in result['directories']], ['도서'])
        with self.assertRaises(SiteError):
            browse_folders(str(self.root / 'escape'), [self.root])
        with self.assertRaises(SiteError):
            browse_folders(str(self.root / '..'), [self.root])

    def test_nas_korean_path_and_parent(self):
        path = self.root / '도서'
        path.mkdir()
        result = browse_folders(str(path), [self.root])
        self.assertEqual(result['path'], str(path))
        self.assertEqual(result['parent'], str(self.root))

    def test_probe_missing_folder_does_not_create_it_or_touch_files(self):
        file = self.root / 'existing.txt'
        file.write_text('original')
        requested = self.root / '도서' / '북토끼'
        result = check_storage(str(requested))
        self.assertFalse(result['exists'])
        self.assertFalse(requested.exists())
        self.assertEqual(list(self.root.iterdir()), [file])
        self.assertEqual(file.read_text(), 'original')

    def test_probe_readonly_failure(self):
        with patch('newtoki_downloader.storage.tempfile.TemporaryFile', side_effect=PermissionError()):
            with self.assertRaises(SiteError):
                check_storage(str(self.root))

    def test_file_in_folder_path_rejected(self):
        file = self.root / 'existing.txt'
        file.write_text('original')
        with self.assertRaises(SiteError):
            check_storage(str(file / 'child'))


if __name__ == '__main__':
    unittest.main()
