from pathlib import Path
import json
import os
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from moonlyrics import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.preferred = self.root / "AppData" / "MoonLyrics"
        self.application = self.root / "portable-app"
        self.portable = self.application / "MoonLyrics-data"

    def test_uses_normal_profile_without_leaving_probe_files(self):
        self.assertEqual(storage.data_directory(self.preferred, self.application), self.preferred)
        self.assertEqual(list(self.preferred.iterdir()), [])
        self.assertFalse(self.portable.exists())

    def test_single_file_payload_keeps_its_profile_beside_the_outer_executable(self):
        self.application.mkdir()
        payload = self.root / 'disposable' / 'MoonLyrics.exe'
        actual = storage.application_directory(payload, self.root, frozen=True,
                                               launcher_directory=str(self.application))
        self.assertEqual(actual, self.application)
        self.assertEqual(storage.application_directory(payload, self.root, frozen=False,
                         launcher_directory=str(self.application)), self.root)
        for invalid in ('relative-folder', str(self.root / 'missing'), None):
            self.assertEqual(storage.application_directory(payload, self.root, frozen=True,
                             launcher_directory=invalid), payload.parent)

    def test_access_denied_falls_back_to_a_real_writable_portable_folder(self):
        original = storage._check_writable
        def check(directory):
            if directory == self.preferred:
                raise PermissionError("access denied")
            return original(directory)
        with patch.object(storage, "_check_writable", side_effect=check):
            self.assertEqual(storage.data_directory(self.preferred, self.application), self.portable)
        (self.portable / "settings.json").write_text('{"appearance_mode":"night"}', encoding="utf-8")
        # A later launch keeps the same profile even if AppData becomes writable.
        self.assertEqual(storage.data_directory(self.preferred, self.application), self.portable)
        self.assertIn("night", (self.portable / "settings.json").read_text(encoding="utf-8"))

    def test_unwritable_portable_profile_can_fall_back_to_normal_profile(self):
        self.portable.mkdir(parents=True)
        original = storage._check_writable
        def check(directory):
            if directory == self.portable:
                raise PermissionError("access denied")
            return original(directory)
        with patch.object(storage, "_check_writable", side_effect=check):
            self.assertEqual(storage.data_directory(self.preferred, self.application), self.preferred)

    def test_reports_an_actionable_error_if_neither_folder_is_writable(self):
        with patch.object(storage, "_check_writable", side_effect=PermissionError("access denied")):
            with self.assertRaisesRegex(OSError, "extract it to a writable folder"):
                storage.data_directory(self.preferred, self.application)

    def test_file_access_denied_has_only_one_attempt_per_directory(self):
        with patch.object(Path, "open", side_effect=PermissionError("access denied")) as create:
            with self.assertRaises(OSError):
                storage.data_directory(self.preferred, self.application)
        self.assertEqual(create.call_count, 2)


@unittest.skipUnless(os.environ.get("MOONLYRICS_PACKAGED_EXE"), "Opt-in verification of the built Windows EXE")
class PackagedStartupTests(unittest.TestCase):
    def test_copying_only_the_exe_survives_unwritable_temp_and_appdata(self):
        executable = Path(os.environ['MOONLYRICS_PACKAGED_EXE']).resolve()
        build = Path(__file__).resolve().parents[1] / 'build'
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='single-exe-', dir=build) as directory:
            root = Path(directory)
            copied = root / '月光 Copied app'
            copied.mkdir()
            single = copied / 'MoonLyrics 单文件.exe'
            shutil.copyfile(executable, single)
            self.assertEqual(list(copied.iterdir()), [single])
            blocked = root / 'not-a-directory'
            blocked.write_text('not a writable directory', encoding='utf-8')
            environment = dict(os.environ, TEMP=str(blocked), TMP=str(blocked), LOCALAPPDATA=str(blocked))
            profile = copied / 'MoonLyrics-data'
            for run in range(2):
                report = root / f'月光 startup report {run}.json'
                result = subprocess.run([str(single), '--startup-probe', str(report)],
                                        env=environment, cwd=os.environ['SystemRoot'],
                                        timeout=30, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                self.assertTrue(report.is_file(), 'The single-file launcher never reached Python')
                data = json.loads(report.read_text(encoding='utf-8'))
                self.assertTrue(data['ok'] and data['bundled_assets'])
                self.assertEqual(Path(data['application_directory']), copied)
                self.assertEqual(Path(data['data_directory']), profile)
                self.assertFalse(Path(data['executable']).parent.exists(), 'Unpacked dependencies were not cleaned up')
                self.assertFalse((copied / '_internal').exists(), 'The outer EXE depends on a sibling _internal')
                if run == 0:
                    (profile / 'settings.json').write_text('{"appearance_mode":"night"}', encoding='utf-8')
            self.assertIn('night', (profile / 'settings.json').read_text(encoding='utf-8'))

    def test_single_file_uses_writable_appdata_without_requiring_a_writable_working_directory(self):
        executable = Path(os.environ['MOONLYRICS_PACKAGED_EXE']).resolve()
        build = Path(__file__).resolve().parents[1] / 'build'
        with tempfile.TemporaryDirectory(prefix='normal-single-exe-', dir=build) as directory:
            root = Path(directory)
            local = root / 'User AppData'
            temp = root / 'User Temp'
            local.mkdir(); temp.mkdir()
            blocked = root / 'blocked-appdata'
            blocked.write_text('not a directory', encoding='utf-8')
            for index, appdata in enumerate((local, blocked)):
                with self.subTest(appdata_available=appdata == local):
                    # Isolate this check from any portable profile beside the build EXE.
                    copied = root / f'app-{index}'
                    copied.mkdir()
                    single = copied / 'MoonLyrics.exe'
                    shutil.copyfile(executable, single)
                    report = root / f'startup-{index}.json'
                    result = subprocess.run([str(single), '--startup-probe', str(report)],
                        env=dict(os.environ, LOCALAPPDATA=str(appdata), TEMP=str(temp), TMP=str(temp)),
                        cwd=os.environ['SystemRoot'], timeout=30, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                    data = json.loads(report.read_text(encoding='utf-8'))
                    self.assertTrue(data['ok'])
                    profile = local / 'MoonLyrics' if appdata == local else copied / 'MoonLyrics-data'
                    self.assertEqual(Path(data['data_directory']), profile)
                    unpacked = Path(data['executable']).parent
                    expected = local / 'MoonLyrics' / 'runtime' if appdata == local else temp / 'MoonLyrics-runtime'
                    self.assertEqual(unpacked.parent, expected)
                    self.assertFalse(unpacked.exists())

    def test_starts_with_unmodified_temp_and_appdata_environment(self):
        executable = Path(os.environ["MOONLYRICS_PACKAGED_EXE"]).resolve()
        self.assertTrue(executable.is_file())
        build = Path(__file__).resolve().parents[1] / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="packaged-startup-", dir=build) as directory:
            report = Path(directory) / "result.json"
            # Inherit TEMP, TMP and LOCALAPPDATA exactly as a normal launch does.
            # The self-test does not save preferences or control the user's music.
            result = subprocess.run([str(executable), "--self-test", "--report", str(report)],
                                    timeout=90, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            self.assertTrue(report.is_file(), "The EXE never reached the application self-test")
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(data.get("ok"), data.get("error"))
            self.assertTrue(data["appearance_modes"]["system_theme_follow"])
