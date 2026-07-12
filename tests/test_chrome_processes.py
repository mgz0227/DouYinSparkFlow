import unittest
from unittest.mock import patch

import core.chrome_processes as chrome_processes
from core.chrome_processes import (
    ChromeProcessGuard,
    ProcessInfo,
    _linux_stat_details,
    _is_chrome_process,
    _parent_first,
    _process_argument_value,
    _process_executable,
    _process_has_argument,
    _process_title_profile_identity,
    _terminate_linux_process,
    _windows_cim_start_marker,
)


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PLAYWRIGHT_PROFILE = (
    r"C:\Temp\playwright_chromiumdev_profile-fallback-test"
)
LINUX_CHROME = "/opt/google/chrome/chrome"
PROCESS_TITLE_PROFILE = "playwright_chromiumdev_profile-D5pONf"


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


def non_chrome_process(
    pid,
    ppid,
    *,
    start,
    name="python.exe",
    executable=r"C:\Python\python.exe",
):
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        name=name,
        argv=(executable,),
        start_marker=start,
    )


def process_title_chrome(
    pid,
    ppid,
    *,
    start,
    profile=PROCESS_TITLE_PROFILE,
    executable_path=LINUX_CHROME,
    title_suffix="--remote-debugging-pipe --no-first-run",
):
    title = f"{profile} {title_suffix}".rstrip()
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        name="chrome",
        argv=(title,),
        start_marker=start,
        executable_path=executable_path,
    )


def merged_argv_chrome(
    pid,
    ppid,
    *arguments,
    start,
    first_token=LINUX_CHROME,
    executable_path=LINUX_CHROME,
    name="chrome",
):
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        name=name,
        argv=(" ".join((first_token, *arguments)),),
        start_marker=start,
        executable_path=executable_path,
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

    def build_fallback_guard(self, table, anchor_pid=1):
        return ChromeProcessGuard(
            token="run-token",
            process_lister=table.list,
            process_terminator=table.terminate,
            anchor_pid=anchor_pid,
        )

    def test_before_launch_snapshot_records_anchor_and_existing_chrome(self):
        anchor = non_chrome_process(1, 0, start="100")
        user_chrome = process(
            50,
            5,
            r"--user-data-dir=C:\Users\me\Chrome",
            start="90",
        )
        table = FakeProcessTable([anchor, user_chrome])
        guard = self.build_fallback_guard(table)

        captured_anchor = guard.capture_before_launch()

        self.assertEqual(captured_anchor, anchor)
        self.assertEqual(guard.anchor_start_marker, "100")
        self.assertEqual(
            guard.preexisting_chrome_fingerprints,
            {50: "90"},
        )

    def test_fallback_captures_only_new_anchor_descendant_chrome(self):
        anchor = non_chrome_process(1, 0, start="100")
        driver = non_chrome_process(
            10,
            1,
            start="110",
            name="node.exe",
            executable=r"C:\Playwright\node.exe",
        )
        user_chrome = process(
            50,
            5,
            r"--user-data-dir=C:\Users\me\Chrome",
            start="90",
        )
        table = FakeProcessTable([anchor, driver, user_chrome])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        root = process(
            100,
            10,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="120",
        )
        child = process(101, 100, "--type=renderer", start="130")
        concurrent_chrome = process(
            200,
            20,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="125",
        )
        table.processes = [
            anchor,
            driver,
            user_chrome,
            root,
            child,
            concurrent_chrome,
        ]

        owned = guard.capture_after_launch(timeout=0.1)

        self.assertEqual({item.pid for item in owned}, {100, 101})
        self.assertEqual(
            set(guard.process_fingerprints),
            {100, 101},
        )
        self.assertNotIn(50, guard.process_fingerprints)
        self.assertNotIn(200, guard.process_fingerprints)
        table.processes.append(
            process(
                201,
                200,
                f"--user-data-dir={PLAYWRIGHT_PROFILE}",
                start="135",
            )
        )
        self.assertEqual(guard.current_owned_processes(), [root, child])

    def test_token_path_rejects_profile_used_before_launch(self):
        anchor = non_chrome_process(1, 0, start="100")
        existing = process(
            50,
            5,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="90",
        )
        table = FakeProcessTable([anchor, existing])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        token_root = process(
            100,
            1,
            guard.launch_argument,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="120",
        )
        table.processes = [anchor, existing, token_root]

        with self.assertRaisesRegex(
            RuntimeError,
            "profile 在启动前已被其他 Chrome 使用",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_token_path_keeps_profile_matching_without_snapshot(self):
        table = FakeProcessTable([])
        guard = self.build_guard(table)
        root = process(
            100,
            10,
            guard.launch_argument,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="120",
        )
        reparented_profile_process = process(
            101,
            1,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="130",
        )
        table.processes = [root, reparented_profile_process]

        owned = guard.capture_after_launch(timeout=0.1)

        self.assertEqual({item.pid for item in owned}, {100, 101})

    def test_fallback_tracks_reparented_known_child_and_later_child(self):
        anchor = non_chrome_process(1, 0, start="100")
        driver = non_chrome_process(
            10,
            1,
            start="110",
            name="node.exe",
            executable=r"C:\Playwright\node.exe",
        )
        table = FakeProcessTable([anchor, driver])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        root = process(
            100,
            10,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="120",
        )
        child = process(101, 100, "--type=gpu-process", start="130")
        table.processes = [anchor, driver, root, child]
        guard.capture_after_launch(timeout=0.1)
        reparented_child = process(
            101,
            1,
            "--type=gpu-process",
            start="130",
        )
        later_child = process(102, 101, "--type=renderer", start="140")
        table.processes = [anchor, driver, reparented_child, later_child]

        owned_before_close = guard.capture_before_close()
        terminated = guard.cleanup(timeout=0)

        self.assertEqual(
            {item.pid for item in owned_before_close},
            {101, 102},
        )
        self.assertEqual(terminated, [101, 102])
        self.assertEqual(table.termination_calls, [({101, 102}, False)])

    def test_linux_process_title_fallback_uses_proc_exe_and_anchor_tree(self):
        anchor = non_chrome_process(1, 0, start="100")
        driver = non_chrome_process(
            10,
            1,
            start="110",
            name="node",
            executable="/opt/playwright/node",
        )
        table = FakeProcessTable([anchor, driver])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        root = process_title_chrome(100, 10, start="120")
        child = process_title_chrome(101, 100, start="130")
        concurrent = process_title_chrome(200, 20, start="125")
        crashpad = process_title_chrome(
            300,
            999,
            start="126",
            executable_path="/opt/google/chrome/chrome_crashpad_handler",
        )
        table.processes = [
            anchor,
            driver,
            root,
            child,
            concurrent,
            crashpad,
        ]

        owned = guard.capture_after_launch(timeout=0.1)

        self.assertEqual({item.pid for item in owned}, {100, 101})
        self.assertEqual(guard.profile_paths, set())
        self.assertEqual(len(guard.profile_identities), 1)
        self.assertIn(
            _process_executable(root),
            guard.chrome_executables,
        )
        self.assertNotIn(200, guard.process_fingerprints)
        self.assertNotIn(300, guard.process_fingerprints)

        reparented_child = process_title_chrome(101, 1, start="130")
        later_child = process_title_chrome(102, 101, start="140")
        table.processes = [
            anchor,
            driver,
            reparented_child,
            later_child,
            concurrent,
            crashpad,
        ]

        self.assertEqual(
            {item.pid for item in guard.capture_before_close()},
            {101, 102},
        )

    def test_merged_single_argv_is_captured_then_tracks_profile_title(self):
        anchor = non_chrome_process(1, 0, start="100")
        driver = non_chrome_process(
            10,
            1,
            start="110",
            name="node",
            executable="/opt/playwright/node",
        )
        table = FakeProcessTable([anchor, driver])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        profile_path = f"/tmp/{PROCESS_TITLE_PROFILE}"
        root = merged_argv_chrome(
            100,
            10,
            "--disable-dev-shm-usage",
            guard.launch_argument,
            f"--user-data-dir={profile_path}",
            "--remote-debugging-pipe",
            start="120",
        )
        child = merged_argv_chrome(
            101,
            100,
            "--type=renderer",
            guard.launch_argument,
            f"--user-data-dir={profile_path}",
            start="130",
        )
        table.processes = [anchor, driver, root, child]

        owned = guard.capture_after_launch(timeout=0.1)

        self.assertEqual({item.pid for item in owned}, {100, 101})
        self.assertTrue(_process_has_argument(root, guard.launch_argument))
        self.assertEqual(
            _process_argument_value(root, "--user-data-dir"),
            profile_path,
        )

        titled_root = process_title_chrome(100, 10, start="120")
        titled_child = process_title_chrome(101, 100, start="130")
        later_child = process_title_chrome(102, 101, start="140")
        table.processes = [
            anchor,
            driver,
            titled_root,
            titled_child,
            later_child,
        ]

        self.assertEqual(
            {item.pid for item in guard.current_owned_processes()},
            {100, 101, 102},
        )

    def test_merged_single_argv_requires_exe_matching_first_token(self):
        candidate = merged_argv_chrome(
            100,
            1,
            "--dysf-run-token=run-token",
            f"--user-data-dir=/tmp/{PROCESS_TITLE_PROFILE}",
            start="120",
            first_token="/tmp/chrome",
        )

        self.assertFalse(
            _process_has_argument(candidate, "--dysf-run-token=run-token")
        )
        self.assertIsNone(
            _process_argument_value(candidate, "--user-data-dir")
        )

    def test_chrome_detection_rejects_deceptive_basenames(self):
        deceptive = [
            "/tmp/chromedriver",
            "/tmp/chromedriver.exe",
            "/tmp/notchrome-helper",
            "/tmp/chrome-helper",
        ]

        for index, executable_path in enumerate(deceptive, start=100):
            with self.subTest(executable_path=executable_path):
                candidate = ProcessInfo(
                    pid=index,
                    ppid=1,
                    name="chrome",
                    argv=(executable_path,),
                    start_marker=str(index * 10),
                    executable_path=executable_path,
                )
                self.assertFalse(_is_chrome_process(candidate))

        allowed = [
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/opt/google/chrome/chrome_crashpad_handler",
        ]

        for index, executable_path in enumerate(allowed, start=200):
            with self.subTest(executable_path=executable_path):
                candidate = ProcessInfo(
                    pid=index,
                    ppid=1,
                    name="ignored",
                    argv=(executable_path,),
                    start_marker=str(index * 10),
                    executable_path=executable_path,
                )
                self.assertTrue(_is_chrome_process(candidate))

    def test_process_title_profile_requires_one_strict_argv(self):
        valid = process_title_chrome(100, 1, start="120")
        malformed_profiles = [
            "prefix-playwright_chromiumdev_profile-D5pONf",
            "playwright_chromiumdev_profile-D5pONf7",
            "playwright_chromiumdev_profile-D5pO-f",
            "/tmp/playwright_chromiumdev_profile-D5pONf",
        ]

        self.assertTrue(_process_title_profile_identity(valid))

        for index, profile in enumerate(malformed_profiles, start=101):
            with self.subTest(profile=profile):
                candidate = process_title_chrome(
                    index,
                    1,
                    start=str(index * 10),
                    profile=profile,
                )
                self.assertEqual(
                    _process_title_profile_identity(candidate),
                    "",
                )

        multiple_argv = ProcessInfo(
            pid=200,
            ppid=1,
            name="chrome",
            argv=(PROCESS_TITLE_PROFILE, "--remote-debugging-pipe"),
            start_marker="2000",
            executable_path=LINUX_CHROME,
        )
        self.assertEqual(_process_title_profile_identity(multiple_argv), "")

    def test_process_title_fallback_without_proc_exe_fails_closed(self):
        anchor = non_chrome_process(1, 0, start="100")
        table = FakeProcessTable([anchor])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        table.processes = [
            anchor,
            process_title_chrome(
                100,
                1,
                start="120",
                executable_path="",
            ),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "唯一确定 Chrome 可执行文件",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_process_title_profile_used_before_launch_fails_closed(self):
        anchor = non_chrome_process(1, 0, start="100")
        existing = process_title_chrome(50, 5, start="90")
        table = FakeProcessTable([anchor, existing])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        root = process_title_chrome(100, 1, start="120")
        table.processes = [anchor, existing, root]

        with self.assertRaisesRegex(
            RuntimeError,
            "profile 在启动前已被 Chrome 使用",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_fallback_rejects_profile_used_before_launch(self):
        anchor = non_chrome_process(1, 0, start="100")
        existing = process(
            50,
            5,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="90",
        )
        table = FakeProcessTable([anchor, existing])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        root = process(
            100,
            1,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="120",
        )
        table.processes = [anchor, existing, root]

        with self.assertRaisesRegex(
            RuntimeError,
            "profile 在启动前已被 Chrome 使用",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_fallback_rejects_preexisting_chrome_pid_reuse(self):
        anchor = non_chrome_process(1, 0, start="100")
        existing = process(50, 5, start="90")
        table = FakeProcessTable([anchor, existing])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        reused = process(
            50,
            1,
            f"--user-data-dir={PLAYWRIGHT_PROFILE}",
            start="120",
        )
        table.processes = [anchor, reused]

        with self.assertRaisesRegex(RuntimeError, "PID 已复用"):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(table.termination_calls, [])

    def test_fallback_without_profile_fails_closed(self):
        anchor = non_chrome_process(1, 0, start="100")
        table = FakeProcessTable([anchor])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        table.processes = [
            anchor,
            process(100, 1, "--headless", start="120"),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "唯一确定 Playwright 临时 profile",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_fallback_missing_start_marker_fails_closed(self):
        anchor = non_chrome_process(1, 0, start="100")
        table = FakeProcessTable([anchor])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        table.processes = [
            anchor,
            ProcessInfo(
                pid=100,
                ppid=1,
                name="chrome.exe",
                argv=(CHROME, f"--user-data-dir={PLAYWRIGHT_PROFILE}"),
                start_marker="",
            ),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            r"启动后 Chrome 进程创建时间: \[100\]",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(table.termination_calls, [])

    def test_fallback_rejects_reused_anchor_pid(self):
        anchor = non_chrome_process(1, 0, start="100")
        table = FakeProcessTable([anchor])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        reused_anchor = non_chrome_process(1, 0, start="101")
        table.processes = [
            reused_anchor,
            process(
                100,
                1,
                f"--user-data-dir={PLAYWRIGHT_PROFILE}",
                start="120",
            ),
        ]

        with self.assertRaisesRegex(RuntimeError, "锚点进程 PID 已复用"):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_fallback_rejects_chrome_older_than_anchor(self):
        anchor = non_chrome_process(1, 0, start="100")
        table = FakeProcessTable([anchor])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        table.processes = [
            anchor,
            process(
                100,
                1,
                f"--user-data-dir={PLAYWRIGHT_PROFILE}",
                start="90",
            ),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "无法建立本次 Chrome 进程归属",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_fallback_rejects_multiple_profiles(self):
        anchor = non_chrome_process(1, 0, start="100")
        table = FakeProcessTable([anchor])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        table.processes = [
            anchor,
            process(
                100,
                1,
                f"--user-data-dir={PLAYWRIGHT_PROFILE}",
                start="120",
            ),
            process(
                101,
                1,
                "--user-data-dir="
                r"C:\Temp\playwright_chromiumdev_profile-second",
                start="121",
            ),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "唯一确定 Playwright 临时 profile",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

    def test_fallback_rejects_multiple_profile_executables(self):
        anchor = non_chrome_process(1, 0, start="100")
        table = FakeProcessTable([anchor])
        guard = self.build_fallback_guard(table)
        guard.capture_before_launch()
        table.processes = [
            anchor,
            process(
                100,
                1,
                f"--user-data-dir={PLAYWRIGHT_PROFILE}",
                start="120",
            ),
            process(
                101,
                1,
                f"--user-data-dir={PLAYWRIGHT_PROFILE}",
                start="121",
                executable=r"C:\Chromium\chromium.exe",
            ),
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "唯一确定 Chrome 可执行文件",
        ):
            guard.capture_after_launch(timeout=0.1)

        self.assertEqual(guard.process_fingerprints, {})

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
