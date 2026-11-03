"""验证网络缺失的 Docker 输出不会阻断沙盒恢复。"""

import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from deerflow.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider
from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend, _is_no_such_network_error
from deerflow.community.aio_sandbox.quarantine import SandboxQuarantine
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo


class SandboxNetworkCleanupTests(unittest.TestCase):
    def setUp(self):
        self.backend = LocalContainerBackend.__new__(LocalContainerBackend)
        self.backend._runtime = "docker"
        self.backend._network_mode = "open"
        self.backend._container_prefix = "deer-flow-sandbox"
        self.backend._pending_cleanup_ports_lock = threading.Lock()
        self.backend._pending_cleanup_ports = {"generation": ("sandbox", 10000)}
        self.backend._unknown_cleanup_ports = {}
        _, self.network_name = self.backend._resource_names("sandbox")
        self.egress_name = self.backend._egress_network_name("sandbox")
        self.enterContext(patch.object(self.backend, "_remove_restricted_proxy"))
        self.release_port = self.enterContext(patch("deerflow.community.aio_sandbox.local_backend.release_port"))

    def missing_network_result(self, command, **kwargs):
        message = f"Error response from daemon: network {command[-1]} not found\nexit status 1\n"
        return CompletedProcess(command, 1, "", message)

    def prepare_quarantined_provider(self):
        directory = self.enterContext(TemporaryDirectory())
        quarantine = SandboxQuarantine(Path(directory), "test")
        info = SandboxInfo("sandbox", "", container_id="generation")
        quarantine.mark(info)
        provider = AioSandboxProvider.__new__(AioSandboxProvider)
        provider._backend = self.backend
        self.enterContext(patch.object(provider, "_quarantine_store", return_value=quarantine))
        self.enterContext(patch.object(provider, "_reserve_local_teardown", return_value=True))
        self.enterContext(patch.object(provider, "_claim_ownership", return_value=True))
        self.enterContext(patch.object(provider, "_held_teardown_lease", side_effect=lambda _: nullcontext()))
        self.enterContext(patch.object(provider, "_finish_local_teardown"))
        self.enterContext(patch.object(self.backend, "is_absent", return_value=True))
        return provider, quarantine, info

    def test_accepts_exact_missing_network_with_optional_cli_summary(self):
        messages = (
            f"Error response from daemon: network {self.network_name} not found",
            f"ERROR RESPONSE FROM DAEMON: NETWORK {self.network_name} NOT FOUND",
            f"error: no such network: {self.network_name}",
            f"No such network: {self.network_name}",
        )
        for message in messages:
            for ending in ("", "\n", "\nexit status 1\n", "\r\nexit status 1\r\n"):
                with self.subTest(message=message, ending=ending):
                    self.assertTrue(_is_no_such_network_error(message + ending, self.network_name))

    def test_rejects_wrong_identity_and_additional_errors(self):
        missing = f"Error response from daemon: network {self.network_name} not found"
        messages = (
            missing.replace(self.network_name, self.network_name.upper()),
            missing.replace(self.network_name, self.egress_name),
            f"Error response from daemon: network {self.network_name} has active endpoints\nexit status 1",
            f"unable to resolve docker endpoint: context {self.network_name} not found",
            "Cannot connect to the Docker daemon\nexit status 1",
            missing + "\npermission denied\nexit status 1",
            missing + f"\nError response from daemon: network {self.egress_name} not found\nexit status 1",
            missing + "\nexit status 1\npermission denied",
            missing + "\n\nexit status 1",
            missing + "\nexit status 1\nexit status 1",
            missing + "\nexit status 2",
            "exit status 1",
            "",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(_is_no_such_network_error(message, self.network_name))

    def test_open_mode_recovery_retires_quarantine_when_networks_are_absent(self):
        provider, quarantine, info = self.prepare_quarantined_provider()
        with patch("deerflow.community.aio_sandbox.local_backend.subprocess.run", side_effect=self.missing_network_result) as run:
            provider._retire_absent_quarantine("sandbox")
        self.assertFalse(quarantine.contains(info))
        self.assertEqual([call.args[0] for call in run.call_args_list], [["docker", "network", "rm", name] for name in (self.network_name, self.egress_name)])
        self.release_port.assert_called_once_with(10000)
        self.assertEqual(self.backend._pending_cleanup_ports, {})

    def test_fresh_thread_skips_historical_resource_cleanup(self):
        provider, quarantine, info = self.prepare_quarantined_provider()
        quarantine.retire(info)
        with patch.object(self.backend, "complete_absent_teardown") as cleanup:
            provider._retire_absent_quarantine("sandbox")
        cleanup.assert_not_called()
        self.backend.is_absent.assert_not_called()

    def test_unknown_cleanup_failure_retains_quarantine_and_port(self):
        provider, quarantine, info = self.prepare_quarantined_provider()
        failure = CompletedProcess([], 1, "", "Cannot connect to the Docker daemon\nexit status 1\n")
        results = [self.missing_network_result([self.network_name]), failure]
        with patch("deerflow.community.aio_sandbox.local_backend.subprocess.run", side_effect=results):
            with self.assertRaisesRegex(RuntimeError, self.egress_name):
                provider._retire_absent_quarantine("sandbox")
        self.assertTrue(quarantine.contains(info))
        self.release_port.assert_not_called()
        self.assertEqual(self.backend._pending_cleanup_ports, {"generation": ("sandbox", 10000)})


if __name__ == "__main__":
    unittest.main()
