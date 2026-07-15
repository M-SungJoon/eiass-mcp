# -*- coding: utf-8 -*-
import subprocess
import sys
import unittest


class StdioTests(unittest.TestCase):
    def test_normal_stdin_close_has_no_traceback(self):
        process = subprocess.Popen([sys.executable, 'mcp_server.py'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdin.close()
        process.wait(timeout=15)
        stderr = process.stderr.read().decode('utf-8', errors='replace')
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.returncode, 0)
        self.assertNotIn('Traceback', stderr)


if __name__ == '__main__':
    unittest.main()
