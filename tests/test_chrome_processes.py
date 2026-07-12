import unittest
from unittest.mock import patch

import core.chrome_processes as chrome_processes
from core.chrome_processes import (
    ChromeProcessGuard,
    ProcessInfo,
    _linux_stat_details,
    _parent_first,
    _terminate_linux_process,
    _windows_cim_start_marker,
)


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def process(
    pid,
    ppid,
    *arguments,
    start=None,
    name="chrome.exe",
    executable=CHROME,
):
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        name=name,
        argv=(executable, *arguments),
        start_marker=start or str(pid * 10),
    )


class FakeProcessTable:
    def __init__(self, processes, remove_on_terminate=True):
        self.processes = list(processes)
        self.remove_on_terminate = remove_on_terminate
        self.termination_calls = []

    def list(self):
        return list(self.processes)

    def terminate(self, targets, force=False):
        target_pids = {target.pid for target in targets}
        self.termination_calls.append((target_pids, force))

        if self.remove_on_terminate:
            self.processes = [
                item
                for item in self.processes
                if item.pid not in target_pids
            ]


class SequencedProcessTable(FakeProcessTable):
    def __init__(self, snapshots):
        super().__init__([])
        self.snapshots = [list(snapshot) for snapshot in snapshots]

    def list(self):
        if self.snapshots:
            self.processes = self.snapshots.pop(0)

        return super().list()


class ChromeProcessGuardTests(unittest.TestCase):
    def build_guard(self, table):
        return ChromeProcessGuard(
            token="run-token",
            process_lister=table.list,
            process_terminator=table.terminate,
        )

    def test_capture_excludes_preexisting_user_chrome(self):
        table = FakeProcessTable([])
        guard = self.build_guard(table)
        table.processes = [
            process(
                100,
                10,
                guard.launch_argument,
                r"--user-data-dir=C:\Temp\playwright-profile",
            ),
            process(
                101,
                100,
                "--type=renderer",
                r"--user-data-dir=C:\Temp\playwright-profile",
            ),
            process(
                200,
                20,
                r"--user-data-dir=C:\Users\me\Chrome",
            ),
        ]

        owned = guard.capture_after_launch(timeout=0.1)

        self.assertEqual({item.pid for item in owned}, {100, 101})
        self.assertNotIn(200, guard.process_fingerprints)

    def test_termination_order_stops_root_before_children(self):
        root = process(100, 10, start="1000")
        child = process(101, 100, start="1100")
        grandchild = process(102, 101, start="1200")

        ordered = _parent_first([grandchild, child, root])

        self.assertEqual([item.pid for item in ordered], [100, 101, 102])

    def test_linux_stat_fields_are_read_from_one_snapshot(self):
        fields = ["S", "42", *(["0"] * 17), "777"]
        stat_text = f"123 (chrome helper) {' '.join(fields)}"

        self.assertEqual(
            _linux_stat_details(stat_text),
            ("chrome helper", 42, "777"),
        )

    def test_windows_cim_marker_uses_unix_milliseconds(self):
        self.assertEqual(
            _windows_cim_start_marker("/Date(1783867473726)/"),
            "1783867473726",
        )

    def test_linux_without_pidfd_fails_closed(self):
        candidate = process(100, 10, start="1000")

        with patch.object(
            chrome_processes.os,
            "pidfd_open",
            None,
            create=True,
        ), patch.object(
            chrome_processes.signal,
            "pidfd_send_signal",
            None,
            create=True,
        ), patch.object(chrome_processes.os, "kill", create=True) as kill:
            _terminate_linux_process(candidate, 15)

        kill.assert_not_called()

    def test_reparented_known_child_is_still_cleaned(self):
        table = FakeProcessTable([])
        guard = self.build_guard(table)
        root = process(
            100,
            10,
            guard.launch_argument,
            r"--user-data-dir=C:\Temp\playwright-profile",
        )
        child_executable = r"C:\Program Files\Google\Chrome\chrome_crashpad.exe"
        child = process(
            101,
            100,
            "--type=crashpad-handler",
            executable=child_executable,
        )
        user_chrome = process(
            200,
            20,
            r"--user-data-dir=C:\Users\me\Chrome",
        )
        table.processes = [root, child, user_chrome]
        guard.capture_after_launch(timeout=0.1)
        guard.capture_before_close()
        table.processes = [
            process(
                101,
                1,
                "--type=crashpad-handler",
                start=child.start_marker,
                executable=child_executable,
            ),
            user_chrome,
        ]

        terminated = guard.cleanup(timeout=0)

        self.assertEqual(terminated, [101])
        self.assertEqual(table.termination_calls, [({101}, False)])
        self.assertEqual([item.pid for item in table.processes], [200])

    def test_reused_pid_with_new_start_marker_is_not_cleaned(self):
        table = FakeProcessTable([])
        guard = self.build_guard(table)
        root = process(
            100,
            10,
            guard.launch_argument,
            r"--user-data-dir=C:\Temp\playwright-profile",
        )
        child = process(101, 100, "--type=renderer")
        table.processes = [root, child]
        guard.capture_after_launch(timeout=0.1)
        table.processes = [
            process(
                101,
                20,
                r"--user-data-dir=C:\Users\me\Chrome",
                start="9999",
            )
        ]

        self.assertEqual(guard.cleanup(timeout=0), [])
        self.assertEqual(table.termination_calls, [])

    def test_stale_parent_pid_does_not_make_old_process_owned(self):
        table = FakeProcessTable([])
        guard = self.build_guard(table)
        new_root = process(
            100,
            10,
            guard.launch_argument,
            r"--user-data-dir=C:\Temp\playwright-profile",
            start="5000",
        )
        older_unrelated_chrome = process(
            200,
            100,
            "--type=renderer",
            start="4000",
        )
        table.processes = [new_root, older_unrelated_chrome]

        owned = guard.capture_after_launch(timeout=0.1)

        self.assertEqual([item.pid for item in owned], [100])
        self.assertNotIn(200, guard.process_fingerprints)

    def test_cleanup_is_idempotent(self):
        table = FakeProcessTable([])
        guard = self.build_guard(table)
        table.processes = [
            process(
                100,
                10,
                guard.launch_argument,
                r"--user-data-dir=C:\Temp\playwright-profile",
            )
        ]
        guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.cleanup(timeout=0), [100])
        self.assertEqual(guard.cleanup(timeout=0), [])
        self.assertEqual(len(table.termination_calls), 1)

    def test_process_exit_during_revalidation_is_not_a_cleanup_error(self):
        guard_token = "--dysf-run-token=run-token"
        root = process(
            100,
            10,
            guard_token,
            r"--user-data-dir=C:\Temp\playwright-profile",
        )
        table = SequencedProcessTable([[root], [root], []])
        guard = self.build_guard(table)
        guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.cleanup(timeout=0), [])
        self.assertEqual(table.termination_calls, [])

    def test_no_exact_token_match_fails_closed(self):
        table = FakeProcessTable(
            [process(100, 10, "prefix--dysf-run-token=run-token")]
        )
        guard = self.build_guard(table)

        with self.assertRaisesRegex(
            RuntimeError,
            "无法建立本次 Chrome 进程归属",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(table.termination_calls, [])

    def test_cleanup_reports_process_that_survives_force(self):
        table = FakeProcessTable([], remove_on_terminate=False)
        guard = self.build_guard(table)
        table.processes = [
            process(
                100,
                10,
                guard.launch_argument,
                r"--user-data-dir=C:\Temp\playwright-profile",
            )
        ]
        guard.capture_after_launch(timeout=0.1)

        with patch("core.chrome_processes.time.sleep", return_value=None):
            with self.assertRaisesRegex(
                RuntimeError,
                r"仍有残留: \[100\]",
            ):
                guard.cleanup(timeout=0)

        self.assertEqual(
            table.termination_calls,
            [({100}, False), ({100}, True)],
        )

    def test_missing_start_marker_refuses_to_terminate(self):
        table = FakeProcessTable([])
        guard = self.build_guard(table)
        table.processes = [
            ProcessInfo(
                pid=100,
                ppid=10,
                name="chrome.exe",
                argv=(
                    CHROME,
                    guard.launch_argument,
                    r"--user-data-dir=C:\Temp\playwright-profile",
                ),
                start_marker="",
            )
        ]
        with self.assertRaisesRegex(
            RuntimeError,
            r"无法读取本次 Chrome 进程创建时间: \[100\]",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(table.termination_calls, [])


if __name__ == "__main__":
    unittest.main()
