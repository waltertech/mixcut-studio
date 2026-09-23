import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mixcut import runtime


class RuntimeLocations(unittest.TestCase):
    def test_source_checkout_keeps_project_data_location(self):
        with patch.object(runtime, 'frozen', return_value=False), patch.object(runtime.sys, 'platform', 'darwin'):
            self.assertEqual(runtime.resource_root(), runtime.data_root())

    def test_frozen_macos_uses_application_support(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime, 'frozen', return_value=True), \
                patch.object(runtime.sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path('/Users/test')):
            self.assertEqual(Path('/Users/test/Library/Application Support/MixCutStudio'), runtime.data_root())

    def test_data_directory_override_wins(self):
        with patch.dict(os.environ, {runtime.DATA_ENV_VAR: '/tmp/custom-mixcut'}):
            self.assertEqual(Path('/tmp/custom-mixcut'), runtime.data_root())

    def test_version_comes_from_single_version_file(self):
        self.assertEqual('1.1.0', runtime.app_version())


if __name__ == '__main__':
    unittest.main()
