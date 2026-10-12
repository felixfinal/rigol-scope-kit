"""Contract discovery must work before any device has been configured."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from rigol_scope.cli import main


class OfflineContractTests(unittest.TestCase):
    def test_contract_needs_no_config_or_instrument(self):
        output = io.StringIO()
        with patch("sys.argv", ["rigol-scope", "contract"]), \
             patch("rigol_scope.cli.Settings.load", side_effect=AssertionError("configuration accessed")), \
             patch("rigol_scope.service.TCPTransport", side_effect=AssertionError("instrument accessed")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())["contract_version"], "1.0.0")
