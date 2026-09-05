from __future__ import annotations

import time
import unittest

from glm_analysis_agent import GLMClient, GLMConfig


class CircuitBreakerV1276Tests(unittest.TestCase):
    def test_opens_after_three_failures_and_resets_after_cooldown(self):
        client = GLMClient(GLMConfig())
        client._circuit_cooldown_sec = 0.01
        self.assertFalse(client.is_circuit_open())
        client.record_failure()
        client.record_failure()
        self.assertFalse(client.is_circuit_open())
        client.record_failure()
        self.assertTrue(client.is_circuit_open())
        time.sleep(0.02)
        self.assertFalse(client.is_circuit_open())
        self.assertEqual(client._fail_count, 0)

    def test_success_closes_an_open_circuit(self):
        client = GLMClient(GLMConfig())
        client.record_failure()
        client.record_failure()
        client.record_failure()
        self.assertTrue(client.is_circuit_open())
        client.record_success()
        self.assertFalse(client.is_circuit_open())
        self.assertTrue(client.config.paper_trade)
        self.assertFalse(client.config.execution_allowed)


if __name__ == "__main__":
    unittest.main()
