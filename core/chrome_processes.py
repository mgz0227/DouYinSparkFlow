import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    argv: Tuple[str, ...]
    start_marker: str
    executable_path: str = ""


def _windows_command_line_to_argv(command_line):
    if not command_line:
        return ()

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.CommandLineToArgvW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    argc = ctypes.c_int()
    argv_pointer = shell32.CommandLineToArgvW(
        command_line,
        ctypes.byref(argc),
    )

    if not argv_pointer:
        return ()

    try:
        return tuple(argv_pointer[index] for index in range(argc.value))
    finally:
        kernel32.LocalFree(ctypes.cast(argv_pointer, ctypes.c_void_p))


def _list_windows_processes():
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine,CreationDate | "
        "ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = result.stdout.strip().lstrip("\ufeff")

    if not output:
        return []

    raw_processes = json.loads(output)

    if isinstance(raw_processes, dict):
        raw_processes = [raw_processes]

    processes = []

    for raw in raw_processes:
        try:
            pid = int(raw.get("ProcessId"))
            ppid = int(raw.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            continue

        processes.append(
            ProcessInfo(
                pid=pid,
                ppid=ppid,
                name=str(raw.get("Name") or ""),
                argv=_windows_command_line_to_argv(raw.get("CommandLine") or ""),
                start_marker=_windows_cim_start_marker(raw.get("CreationDate")),
            )
        )

    return processes


def _linux_stat_details(stat_text):
    opening_parenthesis = stat_text.find("(")
    closing_parenthesis = stat_text.rfind(")")

    if opening_parenthesis < 0 or closing_parenthesis <= opening_parenthesis:
        return "", 0, ""

    fields_after_name = stat_text[closing_parenthesis + 2 :].split()

    # /proc/<pid>/stat field 22 is process start time. The sliced list starts
    # at field 3, so its zero-based index is 19.
    if len(fields_after_name) <= 19:
        return "", 0, ""

    try:
        ppid = int(fields_after_name[1])
    except (TypeError, ValueError):
        return "", 0, ""

    name = stat_text[opening_parenthesis + 1 : closing_parenthesis]
    return name, ppid, fields_after_name[19]


def _linux_process_start_marker(stat_text):
    return _linux_stat_details(stat_text)[2]


def _linux_start_marker_for_pid(pid):
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ""

    return _linux_process_start_marker(stat_text)


def _windows_cim_start_marker(value):
    value = str(value or "")
    prefix = "/Date("
    suffix = ")/"

    if not value.startswith(prefix) or not value.endswith(suffix):
        return ""

    milliseconds = value[len(prefix) : -len(suffix)]
    milliseconds = milliseconds.split("+", 1)[0].split("-", 1)[0]
    return milliseconds if milliseconds.isdigit() else ""


def _list_linux_processes():
    processes = []

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue

        try:
            pid = int(entry.name)
            stat_text_before = (entry / "stat").read_text(
                encoding="utf-8",
                errors="replace",
            )
            raw_argv = (entry / "cmdline").read_bytes().split(b"\0")
            argv = tuple(
                os.fsdecode(value)
                for value in raw_argv
                if value
            )
            try:
                executable_path = os.fsdecode(os.readlink(entry / "exe"))
            except OSError:
                executable_path = ""
            stat_text_after = (entry / "stat").read_text(
                encoding="utf-8",
                errors="replace",
            )
            _, _, start_marker_before = _linux_stat_details(stat_text_before)
            name, ppid, start_marker = _linux_stat_details(stat_text_after)

            if not start_marker or start_marker_before != start_marker:
                continue

            processes.append(
                ProcessInfo(
                    pid=pid,
                    ppid=ppid,
                    name=name,
                    argv=argv,
                    start_marker=start_marker,
                    executable_path=executable_path,
                )
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue

    return processes


def list_processes():
    if os.name == "nt":
        return _list_windows_processes()

    if sys.platform.startswith("linux"):
        return _list_linux_processes()

    raise RuntimeError(
        f"当前平台不支持 Chrome 进程兜底清理: {sys.platform}"
    )


_CHROME_BASENAMES = frozenset(
    {
        "chrome",
        "chrome.exe",
        "chrome_crashpad",
        "chrome_crashpad_handler",
        "chrome_crashpad_handler.exe",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium.exe",
        "chromium-browser",
        "chromium_crashpad",
        "chromium_crashpad_handler",
        "chromium_crashpad_handler.exe",
    }
)


def _executable_basename(value):
    value = str(value or "").strip().replace("\\", "/").rstrip("/")
    return value.rsplit("/", 1)[-1].casefold() if value else ""


def _is_chrome_basename(value):
    return _executable_basename(value) in _CHROME_BASENAMES


def _is_chrome_process(process):
    if process.executable_path:
        return _is_chrome_basename(process.executable_path)

    if _is_chrome_basename(process.name):
        return True

    if not process.argv:
        return False

    # A single argv containing whitespace may be Chrome's merged process title,
    # not an executable path. Only /proc/exe-backed parsing may trust it.
    if len(process.argv) == 1 and len(process.argv[0].split()) != 1:
        return False

    return _is_chrome_basename(process.argv[0])


def _argument_value(argv, name):
    prefix = f"{name}="

    for index, argument in enumerate(argv):
        if argument.startswith(prefix):
            return argument[len(prefix) :]

        if argument == name and index + 1 < len(argv):
            return argv[index + 1]

    return None


def _normalize_profile(value):
    value = str(value or "").strip()

    if not value:
        return ""

    return os.path.normcase(os.path.normpath(value))


def _normalize_executable(value):
    value = str(value or "").strip()

    if not value:
        return ""

    return os.path.normcase(os.path.abspath(value))


def _trusted_merged_argv(process):
    if len(process.argv) != 1 or not process.executable_path:
        return ()

    if not _is_chrome_basename(process.executable_path):
        return ()

    raw_command_line = str(process.argv[0] or "")
    arguments = tuple(raw_command_line.split())

    if len(arguments) < 2:
        return ()

    if (
        _normalize_executable(arguments[0])
        != _normalize_executable(process.executable_path)
    ):
        return ()

    return arguments


def _process_has_argument(process, argument):
    merged_argv = _trusted_merged_argv(process)
    return argument in (merged_argv or process.argv)


def _process_argument_value(process, name):
    merged_argv = _trusted_merged_argv(process)

    if not merged_argv:
        return _argument_value(process.argv, name)

    prefix = f"{name}="
    matches = [
        argument[len(prefix) :]
        for argument in merged_argv[1:]
        if argument.startswith(prefix) and argument[len(prefix) :]
    ]
    return matches[0] if len(matches) == 1 else None


def _is_playwright_temporary_profile(value):
    value = str(value or "").strip().replace("\\", "/").rstrip("/")

    if not value:
        return False

    directory_name = value.rsplit("/", 1)[-1].casefold()
    return directory_name.startswith("playwright_chromiumdev_profile-")


_PLAYWRIGHT_PROFILE_IDENTITY_PREFIX = "playwright-profile:"
_PLAYWRIGHT_PROCESS_TITLE_PROFILE = re.compile(
    r"playwright_chromiumdev_profile-[A-Za-z0-9]{6}"
)


def _profile_basename(value):
    value = str(value or "").strip().replace("\\", "/").rstrip("/")
    return value.rsplit("/", 1)[-1] if value else ""


def _explicit_profile_identity(value):
    normalized_profile = _normalize_profile(value)

    if not normalized_profile:
        return ""

    if _is_playwright_temporary_profile(normalized_profile):
        profile_name = os.path.normcase(_profile_basename(normalized_profile))
        return f"{_PLAYWRIGHT_PROFILE_IDENTITY_PREFIX}{profile_name}"

    return f"profile-path:{normalized_profile}"


def _process_title_profile_identity(process):
    if len(process.argv) != 1:
        return ""

    title = str(process.argv[0] or "")

    if not title or title[0].isspace():
        return ""

    first_token = title.split(maxsplit=1)[0]

    if not _PLAYWRIGHT_PROCESS_TITLE_PROFILE.fullmatch(first_token):
        return ""

    profile_name = os.path.normcase(first_token)
    return f"{_PLAYWRIGHT_PROFILE_IDENTITY_PREFIX}{profile_name}"


def _process_profile_identity(process, allow_process_title=False):
    explicit_profile = _process_argument_value(process, "--user-data-dir")
    identity = _explicit_profile_identity(explicit_profile)

    if identity or not allow_process_title:
        return identity

    return _process_title_profile_identity(process)


def _process_executable(process):
    if process.executable_path:
        return _normalize_executable(process.executable_path)

    if (
        not process.argv
        or _process_title_profile_identity(process)
        or (len(process.argv) == 1 and len(process.argv[0].split()) != 1)
    ):
        return ""

    return _normalize_executable(process.argv[0])


def _numeric_start_marker(process):
    try:
        return int(process.start_marker)
    except (TypeError, ValueError):
        return None


def _descendant_ids(processes, roots):
    descendants = {
        root.pid: _numeric_start_marker(root)
        for root in roots
        if _numeric_start_marker(root) is not None
    }

    while True:
        previous_count = len(descendants)

        for process in processes:
            parent_start = descendants.get(process.ppid)
            process_start = _numeric_start_marker(process)

            if (
                parent_start is not None
                and process_start is not None
                and process_start >= parent_start
            ):
                descendants[process.pid] = process_start

        if len(descendants) == previous_count:
            return set(descendants)


def _parent_first(processes):
    by_pid = {process.pid: process for process in processes}

    def depth(process):
        result = 0
        parent_pid = process.ppid
        visited = set()

        while parent_pid in by_pid and parent_pid not in visited:
            visited.add(parent_pid)
            result += 1
            parent_pid = by_pid[parent_pid].ppid

        return result

    return sorted(processes, key=depth)


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("low", ctypes.c_uint32),
        ("high", ctypes.c_uint32),
    ]


def _windows_handle_start_marker(kernel32, handle):
    creation = _FileTime()
    exit_time = _FileTime()
    kernel_time = _FileTime()
    user_time = _FileTime()

    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return ""

    filetime_ticks = (creation.high << 32) | creation.low
    unix_epoch_offset = 116444736000000000
    return str((filetime_ticks - unix_epoch_offset) // 10000)


def _terminate_windows_process(process):
    process_terminate = 0x0001
    synchronize = 0x00100000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_terminate | synchronize | 0x1000,
        False,
        process.pid,
    )

    if not handle:
        return

    try:
        if (
            not process.start_marker
            or _windows_handle_start_marker(kernel32, handle)
            != process.start_marker
        ):
            return

        kernel32.TerminateProcess(handle, 1)
        kernel32.WaitForSingleObject(handle, 1000)
    finally:
        kernel32.CloseHandle(handle)


def _terminate_linux_process(process, selected_signal):
    if not process.start_marker:
        return

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)

    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        return

    try:
        pidfd = pidfd_open(process.pid, 0)
    except OSError:
        return

    try:
        if _linux_start_marker_for_pid(process.pid) != process.start_marker:
            return

        pidfd_send_signal(pidfd, selected_signal, None, 0)
    except OSError:
        return
    finally:
        os.close(pidfd)


def terminate_processes(processes, force=False):
    # Stop the browser root first so it cannot spawn replacement children while
    # the remaining owned processes are being terminated.
    ordered = _parent_first(processes)

    if os.name == "nt":
        for process in ordered:
            _terminate_windows_process(process)
        return

    selected_signal = signal.SIGKILL if force else signal.SIGTERM

    for process in ordered:
        _terminate_linux_process(process, selected_signal)


class ChromeProcessGuard:
    """Track and terminate only Chrome processes launched by this run."""

    def __init__(
        self,
        token=None,
        process_lister=None,
        process_terminator=None,
        anchor_pid=None,
    ):
        self.token = token or uuid.uuid4().hex
        self.launch_argument = f"--dysf-run-token={self.token}"
        self.anchor_pid = os.getpid() if anchor_pid is None else int(anchor_pid)
        self.anchor_start_marker = ""
        self.preexisting_chrome_fingerprints = {}
        self.preexisting_chrome_profiles = set()
        self.profile_paths = set()
        self.profile_identities = set()
        self.chrome_executables = set()
        self.process_fingerprints = {}
        self.fallback_root_fingerprints = {}
        self.fallback_excluded_fingerprints = {}
        self._fallback_established = False
        self._launch_snapshot_captured = False
        self._list_processes = process_lister or list_processes
        self._terminate_processes = process_terminator or terminate_processes

    def _anchor_process(self, processes, expected_start_marker=None):
        matches = [
            process
            for process in processes
            if process.pid == self.anchor_pid
        ]

        if len(matches) != 1:
            raise RuntimeError(
                f"无法唯一定位 Python 锚点进程: {self.anchor_pid}"
            )

        anchor = matches[0]

        if _numeric_start_marker(anchor) is None:
            raise RuntimeError(
                f"无法读取 Python 锚点进程创建时间: {self.anchor_pid}"
            )

        if (
            expected_start_marker is not None
            and anchor.start_marker != expected_start_marker
        ):
            raise RuntimeError(
                f"Python 锚点进程 PID 已复用: {self.anchor_pid}"
            )

        return anchor

    def capture_before_launch(self):
        if self._launch_snapshot_captured:
            raise RuntimeError("本次 Chrome 启动前快照已记录")

        processes = self._list_processes()
        anchor = self._anchor_process(processes)
        chrome_processes = [
            process
            for process in processes
            if _is_chrome_process(process)
        ]
        missing_start_markers = sorted(
            process.pid
            for process in chrome_processes
            if _numeric_start_marker(process) is None
        )

        if missing_start_markers:
            raise RuntimeError(
                "无法记录启动前 Chrome 进程创建时间: "
                f"{missing_start_markers}"
            )

        self.anchor_start_marker = anchor.start_marker
        self.preexisting_chrome_fingerprints = {
            process.pid: process.start_marker
            for process in chrome_processes
        }
        self.preexisting_chrome_profiles = {
            profile_identity
            for process in chrome_processes
            if (
                profile_identity := _process_profile_identity(
                    process,
                    allow_process_title=True,
                )
            )
        }
        self._launch_snapshot_captured = True
        return anchor

    def _token_roots(self, processes):
        return [
            process
            for process in processes
            if _is_chrome_process(process)
            and _process_has_argument(process, self.launch_argument)
        ]

    def _remember_roots(self, roots):
        for root in roots:
            profile_path = _process_argument_value(root, "--user-data-dir")
            profile_identity = _explicit_profile_identity(profile_path)
            executable = _process_executable(root)

            if profile_identity:
                profile = _normalize_profile(profile_path)

                if (
                    self._launch_snapshot_captured
                    and profile_identity in self.preexisting_chrome_profiles
                ):
                    raise RuntimeError(
                        "本次 Chrome profile 在启动前已被其他 Chrome 使用: "
                        f"{profile}"
                    )

                self.profile_paths.add(profile)
                self.profile_identities.add(profile_identity)

            if executable:
                self.chrome_executables.add(executable)

    def _fallback_roots(self, processes):
        if not self._launch_snapshot_captured:
            raise RuntimeError("未记录 Chrome 启动前进程快照")

        anchor = self._anchor_process(
            processes,
            expected_start_marker=self.anchor_start_marker,
        )
        chrome_processes = [
            process
            for process in processes
            if _is_chrome_process(process)
        ]
        reused_pids = sorted(
            process.pid
            for process in chrome_processes
            if (
                process.pid in self.preexisting_chrome_fingerprints
                and process.start_marker
                != self.preexisting_chrome_fingerprints[process.pid]
            )
        )

        if reused_pids:
            raise RuntimeError(
                f"启动前 Chrome 进程 PID 已复用: {reused_pids}"
            )

        new_chrome_processes = [
            process
            for process in chrome_processes
            if process.pid not in self.preexisting_chrome_fingerprints
        ]
        missing_start_markers = sorted(
            process.pid
            for process in new_chrome_processes
            if _numeric_start_marker(process) is None
        )

        if missing_start_markers:
            raise RuntimeError(
                "无法读取启动后 Chrome 进程创建时间: "
                f"{missing_start_markers}"
            )

        # Do not traverse through a Chrome branch that already existed when the
        # launch snapshot was taken. A pre-existing Playwright driver process is
        # still traversable because it is not itself Chrome.
        eligible_processes = [
            process
            for process in processes
            if not (
                _is_chrome_process(process)
                and process.pid in self.preexisting_chrome_fingerprints
            )
        ]
        descendant_pids = _descendant_ids(eligible_processes, [anchor])
        anchor_start = _numeric_start_marker(anchor)

        return [
            process
            for process in new_chrome_processes
            if (
                process.pid in descendant_pids
                and _numeric_start_marker(process) >= anchor_start
            )
        ]

    def _remember_fallback_roots(self, roots, processes):
        profile_processes = []
        profiles = set()

        for root in roots:
            profile = _process_profile_identity(
                root,
                allow_process_title=True,
            )

            if profile:
                profiles.add(profile)
                profile_processes.append(root)

        if len(profiles) != 1:
            raise RuntimeError(
                "无法从 Python 锚点后代唯一确定 Playwright 临时 profile"
            )

        profile = next(iter(profiles))

        if not profile.startswith(_PLAYWRIGHT_PROFILE_IDENTITY_PREFIX):
            raise RuntimeError(
                f"Python 锚点后代使用的不是 Playwright 临时 profile: {profile}"
            )

        if profile in self.preexisting_chrome_profiles:
            raise RuntimeError(
                f"Playwright 临时 profile 在启动前已被 Chrome 使用: {profile}"
            )

        profile_executables = {
            executable
            for root in profile_processes
            if (executable := _process_executable(root))
        }

        if len(profile_executables) != 1:
            raise RuntimeError(
                "无法从 Python 锚点后代唯一确定 Chrome 可执行文件"
            )

        root_fingerprints = {}
        root_executables = set()

        for root in roots:
            if _numeric_start_marker(root) is None:
                raise RuntimeError(
                    f"无法读取可信 Chrome 进程创建时间: {root.pid}"
                )

            root_fingerprints[root.pid] = root.start_marker
            executable = _process_executable(root)

            if executable:
                root_executables.add(executable)

        trusted_pids = set(root_fingerprints)
        excluded_fingerprints = {
            process.pid: process.start_marker
            for process in processes
            if (
                _is_chrome_process(process)
                and process.pid not in trusted_pids
                and process.pid not in self.preexisting_chrome_fingerprints
                and _numeric_start_marker(process) is not None
            )
        }

        self.profile_identities.add(profile)
        self.profile_paths.update(
            normalized_profile
            for root in profile_processes
            if (
                normalized_profile := _normalize_profile(
                    _process_argument_value(root, "--user-data-dir")
                )
            )
        )
        self.chrome_executables.update(profile_executables)
        self.chrome_executables.update(root_executables)
        self.fallback_root_fingerprints.update(root_fingerprints)
        self.fallback_excluded_fingerprints.update(excluded_fingerprints)
        self.process_fingerprints.update(root_fingerprints)
        self._fallback_established = True

    def _is_excluded_fallback_process(self, process):
        if process.pid not in self.fallback_excluded_fingerprints:
            return False

        # A changed marker is PID reuse. It remains excluded because there is no
        # ownership proof for the replacement process.
        return True

    def _excluded_fallback_pids(self, processes):
        roots = [
            process
            for process in processes
            if (
                self.fallback_excluded_fingerprints.get(process.pid)
                == process.start_marker
            )
        ]
        return _descendant_ids(processes, roots)

    def _known_fallback_roots(self, processes):
        return [
            process
            for process in processes
            if (
                self.fallback_root_fingerprints.get(process.pid)
                == process.start_marker
                and _process_executable(process) in self.chrome_executables
            )
        ]

    def _profile_roots(self, processes, excluded_pids):
        roots = []

        for process in processes:
            if not _is_chrome_process(process):
                continue

            if (
                process.pid in excluded_pids
                or self._is_excluded_fallback_process(process)
            ):
                continue

            known_start_marker = self.process_fingerprints.get(process.pid)

            if (
                known_start_marker is not None
                and known_start_marker != process.start_marker
            ):
                continue

            profile = _process_profile_identity(
                process,
                allow_process_title=True,
            )
            executable = _process_executable(process)

            if (
                profile
                and profile in self.profile_identities
                and executable in self.chrome_executables
            ):
                roots.append(process)

        return roots

    def _owned_processes(self, processes):
        token_roots = self._token_roots(processes)
        self._remember_roots(token_roots)
        excluded_pids = self._excluded_fallback_pids(processes)
        profile_roots = (
            self._profile_roots(processes, excluded_pids)
            if self.profile_identities
            else []
        )
        roots = [
            *token_roots,
            *self._known_fallback_roots(processes),
            *profile_roots,
        ]
        descendant_pids = _descendant_ids(
            processes,
            roots,
        )
        owned = []

        for process in processes:
            if not _is_chrome_process(process):
                continue

            if (
                process.pid in excluded_pids
                or self._is_excluded_fallback_process(process)
            ):
                continue

            executable = _process_executable(process)
            profile = _process_profile_identity(
                process,
                allow_process_title=bool(self.profile_identities),
            )
            profile_matches = bool(
                profile
                and profile in self.profile_identities
                and executable in self.chrome_executables
            )
            known_start_marker = self.process_fingerprints.get(process.pid)

            if (
                known_start_marker is not None
                and known_start_marker != process.start_marker
            ):
                continue

            known_fingerprint_matches = bool(
                process.start_marker
                and known_start_marker == process.start_marker
            )
            direct_ownership_matches = bool(
                _process_has_argument(process, self.launch_argument)
                or profile_matches
                or process.pid in descendant_pids
            )
            known_identity_matches = bool(
                known_fingerprint_matches
                and executable in self.chrome_executables
            )

            if direct_ownership_matches or known_identity_matches:
                owned.append(process)

                if executable:
                    self.chrome_executables.add(executable)

        return owned

    def capture_after_launch(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        last_error = None

        while time.monotonic() < deadline:
            try:
                processes = self._list_processes()
                roots = self._token_roots(processes)

                if roots:
                    self._remember_roots(roots)
                elif not self.profile_identities:
                    roots = self._fallback_roots(processes)

                    if roots:
                        self._remember_fallback_roots(roots, processes)

                if roots or self.profile_identities:
                    if not self.profile_identities:
                        raise RuntimeError(
                            "已定位本次 Chrome，但未读取到 Playwright 临时 profile"
                        )

                    if not self.chrome_executables:
                        raise RuntimeError(
                            "已定位本次 Chrome，但未读取到可执行文件路径"
                        )

                    owned = self._owned_processes(processes)
                    missing_start_markers = sorted(
                        process.pid
                        for process in owned
                        if not process.start_marker
                    )

                    if missing_start_markers:
                        raise RuntimeError(
                            "无法读取本次 Chrome 进程创建时间: "
                            f"{missing_start_markers}"
                        )

                    for process in owned:
                        self.process_fingerprints[process.pid] = process.start_marker

                    return owned
            except Exception as exc:
                last_error = exc

            time.sleep(0.1)

        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"无法建立本次 Chrome 进程归属{detail}")

    def current_owned_processes(self):
        return self._owned_processes(self._list_processes())

    def capture_before_close(self):
        processes = self._list_processes()

        if (
            self._launch_snapshot_captured
            and not self.profile_identities
            and not self._token_roots(processes)
        ):
            roots = self._fallback_roots(processes)

            if roots:
                self._remember_fallback_roots(roots, processes)

        owned = self._owned_processes(processes)

        for process in owned:
            if process.start_marker:
                self.process_fingerprints[process.pid] = process.start_marker

        return owned

    def _revalidate(self, expected_processes):
        expected = {
            process.pid: process
            for process in expected_processes
        }
        current = {
            process.pid: process
            for process in self.current_owned_processes()
        }
        validated = []

        for pid, process in expected.items():
            current_process = current.get(pid)

            if current_process is None:
                continue

            if (
                not process.start_marker
                or current_process.start_marker != process.start_marker
            ):
                continue

            validated.append(current_process)

        return validated, list(current.values())

    def cleanup(self, timeout=3.0):
        targets = self.current_owned_processes()

        if not targets:
            return []

        validated_targets, owned_now = self._revalidate(targets)

        if not validated_targets:
            if not owned_now:
                return []

            raise RuntimeError(
                "发现本次 Chrome 候选进程，但无法复核进程创建时间，拒绝终止"
            )

        target_pids = {process.pid for process in validated_targets}
        self._terminate_processes(validated_targets, force=False)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = self.current_owned_processes()

            if not remaining:
                return sorted(target_pids)

            time.sleep(0.1)

        remaining = self.current_owned_processes()

        if remaining:
            target_pids.update(process.pid for process in remaining)
            self._terminate_processes(remaining, force=True)
            time.sleep(0.2)

        remaining = self.current_owned_processes()

        if remaining:
            remaining_pids = sorted(process.pid for process in remaining)
            raise RuntimeError(
                f"本次 Chrome 进程清理后仍有残留: {remaining_pids}"
            )

        return sorted(target_pids)
