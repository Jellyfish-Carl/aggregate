import errno
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from service.server import _nullable_float_array, find_available_port


class ServerPortSelectionTests(unittest.TestCase):
    def test_find_available_port_skips_occupied_port(self):
        calls = []

        def fake_create_server(_host, port):
            calls.append(port)
            if port == 8765:
                raise OSError(errno.EADDRINUSE, "occupied")
            return SimpleNamespace(server_close=lambda: None)

        with patch("service.server.create_server", side_effect=fake_create_server):
            chosen = find_available_port("127.0.0.1", 8765, scan_limit=2)

        self.assertEqual(chosen, 8766)
        self.assertEqual(calls, [8765, 8766])

    def test_nullable_override_array_requires_exactly_96_points(self):
        valid = _nullable_float_array({"x": ["[null," + ",".join(["1"] * 95) + "]"]}, "x")
        self.assertEqual(len(valid), 96)
        self.assertIsNone(valid[0])
        with self.assertRaises(ValueError):
            _nullable_float_array({"x": ["[null]"]}, "x")


if __name__ == "__main__":
    unittest.main()
