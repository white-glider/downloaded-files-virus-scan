"""Regression tests using harmless scanner/notifier stubs and optional Linux inotify."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get('TEST_BASH', 'bash')


def shell_path(path):
    text = str(path.resolve()).replace('\\', '/')
    return '/' + text[0].lower() + text[2:] if os.name == 'nt' else text


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='download-tests-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.downloads = self.base / 'Downloads with spaces'
        self.downloads.mkdir()
        self.private = self.base / 'temporary files'
        self.private.mkdir()
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.env = dict(os.environ, PATH=shell_path(self.bin) + ':/usr/bin:/bin',
                        TMPDIR=shell_path(self.private), TEST_DIR=shell_path(self.base),
                        XDG_RUNTIME_DIR=shell_path(self.runtime),
                        SCAN_DOWNLOAD_DIR=shell_path(self.downloads))
        startup = self.base / 'bash-env'
        startup.write_text('export PATH="$TEST_PATH"\n', encoding='utf-8')
        self.env.update(BASH_ENV=shell_path(startup), TEST_PATH=self.env['PATH'])
        (self.base / 'events').write_bytes(b'')
        self.stub('inotifywait', '''
if [[ ${1:-} == --help ]]; then echo --no-newline; exit 0; fi
printf '%s\\n' "$@" > "$TEST_DIR/watcher-args"
printf '%s\\n' "$$" > "$TEST_DIR/watcher.pid"
if [[ ${HOLD_WATCH:-0} == 1 ]]; then exec sleep 30; fi
cat "$TEST_DIR/events"
exit "${WATCH_STATUS:-0}"
''')
        self.stub('clamscan', '''
file=${!#}
printf '%s\\0' "$file" >> "$TEST_DIR/scanned"
printf '%s\\0' "$@" >> "$TEST_DIR/scanner-args"
printf '%s\\n' "$$" > "$TEST_DIR/scanner.pid"
printf '%s\\n' "${file}" > "$TEST_DIR/latest-file"
if [[ ${SLOW_SCAN:-0} == 1 && ! -e "$TEST_DIR/first-scan" ]]; then
    touch "$TEST_DIR/first-scan"
    sleep 1
fi
if [[ ${BLOCK_SCAN:-0} == 1 ]]; then exec sleep 30; fi
if [[ ${SCAN_STATUS:-0} == 1 ]]; then printf '%s: Test.Signature FOUND\\n' "$file"; fi
exit "${SCAN_STATUS:-0}"
''')
        self.stub('notify-send', '''
printf '%s\\n' "$@" >> "$TEST_DIR/notifications"
exit "${NOTIFY_STATUS:-0}"
''')
        if os.name == 'nt':
            # Git Bash lacks flock and POSIX permissions. Their real behavior
            # is tested only on Linux; these stubs permit portable logic tests.
            self.stub('flock', 'exit 0\n')
            self.stub('stat', 'echo 700\n')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body, encoding='utf-8', newline='\n')
        path.chmod(0o700)

    def event_files(self, names):
        paths = []
        for name in names:
            path = self.downloads / name
            path.write_text('harmless text', encoding='utf-8')
            paths.append(shell_path(path))
        (self.base / 'events').write_bytes(b''.join(p.encode() + b'\0' for p in paths))
        return paths

    def run_scanner(self):
        return subprocess.run([BASH, shell_path(ROOT / 'scan-download.sh')], env=self.env,
                              capture_output=True, timeout=15)

    def scanned(self):
        path = self.base / 'scanned'
        return path.read_bytes().decode().rstrip('\0').split('\0') if path.exists() else []

    def notifications(self):
        path = self.base / 'notifications'
        return path.read_text() if path.exists() else ''

    def assert_cleaned(self):
        self.assertEqual(list(self.private.glob('scan-download.*')), [])

    def test_paths_and_repeated_events(self):
        paths = self.event_files(['report 2026.pdf', 'two  spaces.txt', '-leading.txt'])
        (self.base / 'events').write_bytes(b''.join(p.encode() + b'\0' for p in paths + paths))
        result = self.run_scanner()
        self.assertEqual(self.scanned(), paths + paths, result.stderr)
        self.assertIn('--monitor', (self.base / 'watcher-args').read_text())
        self.assertIn('--no-newline', (self.base / 'watcher-args').read_text())
        self.assertIn(b'--\0' + paths[0].encode() + b'\0', (self.base / 'scanner-args').read_bytes())
        self.assertNotIn('threat detected', self.notifications())
        self.assert_cleaned()

    @unittest.skipIf(os.name == 'nt', 'These literal filenames require a POSIX filesystem')
    def test_unusual_filenames(self):
        paths = self.event_files(['tab\tname', 'line\nname', 'literal*?[x]', 'back\\slash'])
        self.run_scanner()
        self.assertEqual(self.scanned(), paths)

    def test_scan_errors_and_detection(self):
        self.event_files(['file.txt'])
        for status in (0, 1, 2, 127):
            with self.subTest(status=status):
                (self.base / 'notifications').write_text('')
                self.env['SCAN_STATUS'] = str(status)
                result = self.run_scanner()
                note = self.notifications()
                if status == 1:
                    self.assertIn('Test.Signature', note)
                    self.assertIn('threat detected', note)
                elif status:
                    self.assertIn('not verified clean', note)
                    self.assertIn(b'not verified clean', result.stderr)
                else:
                    self.assertNotIn('threat detected', note)
                    self.assertNotIn('not verified clean', note)
                self.assert_cleaned()

    def test_notifier_failure_is_logged(self):
        self.event_files(['file.txt'])
        self.env.update(SCAN_STATUS='1', NOTIFY_STATUS='1')
        self.assertIn(b'notification failed', self.run_scanner().stderr)

    def test_watcher_exit_is_an_error(self):
        for status in (0, 1, 2):
            self.env['WATCH_STATUS'] = str(status)
            result = self.run_scanner()
            self.assertEqual(result.returncode, 2)
            self.assertIn(b'monitoring stopped', result.stderr)
            self.assert_cleaned()

    def test_missing_download_directory(self):
        self.env['SCAN_DOWNLOAD_DIR'] += '/missing'
        result = self.run_scanner()
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'does not exist', result.stderr)

    def test_old_inotifywait_is_rejected(self):
        self.stub('inotifywait', 'echo "old version"\n')
        self.assertEqual(self.run_scanner().returncode, 2)

    def test_temp_creation_failure(self):
        self.stub('mktemp', 'exit 1\n')
        result = self.run_scanner()
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'private temporary', result.stderr)
        self.assert_cleaned()

    @unittest.skipIf(os.name == 'nt', 'Real Linux locking/signals required')
    def test_lock_exclusion_cleanup_and_restart(self):
        self.env['HOLD_WATCH'] = '1'
        process = self.start()
        self.await_file('watcher.pid')
        lock = self.runtime / 'scan-download' / 'lock'
        inode = lock.stat().st_ino
        second = self.run_scanner()
        self.assertEqual(second.returncode, 0)
        self.assertIn(b'already running', second.stderr)
        self.stop(process)
        self.assertTrue(lock.exists())
        self.assertEqual(lock.stat().st_ino, inode)
        self.assert_cleaned()
        self.env['HOLD_WATCH'] = '0'
        self.event_files(['restart.txt'])
        self.run_scanner()
        self.assertEqual(len(self.scanned()), 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions required')
    def test_fallback_and_unsafe_state_directory(self):
        self.env.pop('XDG_RUNTIME_DIR')
        self.run_scanner()
        state = self.private / ('scan-download-' + str(os.getuid()))
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertTrue((state / 'lock').exists())
        state.chmod(0o755)
        result = self.run_scanner()
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'mode 700', result.stderr)

    def start(self):
        process = subprocess.Popen([BASH, shell_path(ROOT / 'scan-download.sh')],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: self.stop(process) if process.poll() is None else None)
        return process

    def stop(self, process):
        process.terminate()
        process.communicate(timeout=10)

    def await_file(self, name):
        deadline = time.monotonic() + 8
        path = self.base / name
        while not path.exists() or not path.stat().st_size:
            if time.monotonic() > deadline:
                self.fail('Timed out waiting for ' + name)
            time.sleep(0.02)

    @unittest.skipUnless(os.name != 'nt' and shutil.which('inotifywait'), 'Real Linux inotifywait required')
    def test_real_download_during_slow_scan_and_rename(self):
        (self.bin / 'inotifywait').unlink()
        self.env['SLOW_SCAN'] = '1'
        process = self.start()
        # Establish readiness by repeatedly creating one harmless probe until
        # the first scan starts; never assume a fixed watcher startup delay.
        deadline = time.monotonic() + 8
        while not (self.base / 'first-scan').exists():
            (self.downloads / 'probe').write_text('probe')
            if time.monotonic() > deadline:
                self.fail('Real watcher did not start')
            time.sleep(0.02)
        target = self.downloads / 'second file.txt'
        target.write_text('arrives during first scan')
        temporary = self.base / 'outside.txt'
        temporary.write_text('renamed into Downloads')
        renamed = self.downloads / 'renamed file.txt'
        temporary.rename(renamed)
        while not {shell_path(target), shell_path(renamed)}.issubset(self.scanned()):
            if time.monotonic() > deadline:
                self.fail('Lost events during slow scan: ' + repr(self.scanned()))
            time.sleep(0.03)
        self.stop(process)
        self.assertEqual(process.returncode, 143)
        self.assert_cleaned()


if __name__ == '__main__':
    unittest.main()
