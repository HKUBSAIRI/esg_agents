"""Exercise cmd.exe itself: importing the Python entry point does not test a batch launcher."""
import os
from pathlib import Path
import socket
import json
import signal
import subprocess
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "启动审查工作台.bat"


class LauncherFormatTests(unittest.TestCase):
    def test_windows_scripts_use_ascii_and_crlf(self):
        for filename in ("启动审查工作台.bat", "安装全流程环境.bat", "一键分类.bat"):
            raw = (ROOT / filename).read_bytes()
            raw.decode("ascii")
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
            self.assertIn(b"pause", raw.lower())


@unittest.skipUnless(os.name == "nt", "Requires Windows cmd.exe")
class WindowsLauncherTests(unittest.TestCase):
    def test_real_batch_help_works_from_another_directory(self):
        with tempfile.TemporaryDirectory(prefix="esg-launcher-help-") as folder:
            result = subprocess.run(f'call "{LAUNCHER}" --help', shell=True, cwd=folder,
                                    input=b"\r\n", capture_output=True, timeout=20)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("--data-dir", output)
        self.assertIn("Press any key", output)
        self.assertNotIn("not recognized", output)

    def test_startup_error_is_visible_and_exit_code_is_preserved(self):
        result = subprocess.run(f'call "{LAUNCHER}" --port invalid', shell=True, cwd=ROOT,
                                input=b"\r\n", capture_output=True, timeout=20)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Startup failed", output)
        self.assertIn("Press any key", output)

    def test_real_batch_starts_a_reachable_server(self):
        with tempfile.TemporaryDirectory(prefix="esg-launcher-server-") as directory:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            folder = Path(directory)
            command = f'call "{LAUNCHER}" --no-open --port {port} --data-dir "{folder / "database"}"'
            process = subprocess.Popen(command, shell=True, cwd=folder, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            server_pid = None
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "Batch launcher exited before the server became reachable")
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/api/bootstrap", timeout=.5) as response:
                            self.assertEqual(response.status, 200)
                            data = json.loads(response.read())
                            self.assertIn("token", data)
                            server_pid = data["process_id"]
                            break
                    except (URLError, TimeoutError):
                        time.sleep(.1)
                else:
                    self.fail("Server did not become reachable within 15 seconds")
            finally:
                if server_pid:
                    # PID is returned by the server created by this test on its reserved port.
                    # Terminate its Python process, then let the batch's normal pause read stdin.
                    os.kill(server_pid, signal.SIGTERM)
                elif process.poll() is None:
                    process.terminate()
                process.communicate(input=b"\r\n", timeout=10)


if __name__ == "__main__":
    unittest.main()
