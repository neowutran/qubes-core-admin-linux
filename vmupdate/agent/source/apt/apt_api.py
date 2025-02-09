# coding=utf-8
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2022  Piotr Bartman <prbartman@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301,
# USA.

import io
import os
import sys
from pathlib import Path
from typing import Tuple, Callable, Optional

import apt
import apt.progress.base
import apt_pkg

from source.common.stream_redirector import StreamRedirector

from .apt_cli import APTCLI


class APT(APTCLI):
    def __init__(self, loglevel):
        super().__init__(loglevel)
        self.package_manager: str = "apt-get"
        self.apt_cache = apt.Cache()
        self.progress = APTProgressReporter()
        self.captured_stdout = io.BytesIO()
        self.captured_stderr = io.BytesIO()

        # to prevent a warning: `debconf: unable to initialize frontend: Dialog`
        os.environ['DEBIAN_FRONTEND'] = 'noninteractive'

    def refresh(self, hard_fail: bool) -> Tuple[int, str, str]:
        """
        Use package manager to refresh available packages.

        :param hard_fail: raise error if some repo is unavailable
        :return: (exit_code, stdout, stderr)
        """
        stdout = ""
        stderr = ""
        captured_stdout = io.BytesIO()
        captured_stderr = io.BytesIO()
        try:
            with StreamRedirector(captured_stdout, captured_stderr):
                success = self.apt_cache.update(
                    self.progress.update_progress,
                    pulse_interval=1  # microseconds
                    # FIXME update reporting:
                    #  it seems that there is no pulse at all
                )
                self.apt_cache.open()
            ret_code = 0 if success else 1
        except Exception as exc:
            ret_code = 2
            stderr += str(exc)
        captured_stdout.flush()
        captured_stdout.seek(0, io.SEEK_SET)
        stdout += captured_stdout.read().decode()
        captured_stderr.flush()
        captured_stderr.seek(0, io.SEEK_SET)
        stderr += captured_stderr.read().decode()
        return ret_code, stdout, stderr

    def upgrade_internal(self, remove_obsolete: bool) -> Tuple[int, str, str]:
        """
        Use `apt` package to upgrade and track progress.
        """
        stdout = ""
        stderr = ""
        captured_stdout = io.BytesIO()
        captured_stderr = io.BytesIO()
        try:
            self.apt_cache.upgrade(dist_upgrade=remove_obsolete)
            Path(os.path.join(
                apt_pkg.config.find_dir("Dir::Cache::Archives"), "partial")
            ).mkdir(parents=True, exist_ok=True)
            with StreamRedirector(captured_stdout, captured_stderr):
                self.apt_cache.commit(
                    self.progress.fetch_progress,
                    self.progress.upgrade_progress
                )
            ret_code = 0
        except Exception as exc:
            ret_code = 1
            stderr += str(exc)
        captured_stdout.flush()
        captured_stdout.seek(0, io.SEEK_SET)
        stdout += captured_stdout.read().decode()
        captured_stderr.flush()
        captured_stderr.seek(0, io.SEEK_SET)
        stderr += captured_stderr.read().decode()
        return ret_code, stdout, stderr


class APTProgressReporter:
    """
    Simple rough progress reporter.

    It is assumed that updating takes 2%, fetching packages takes 49% and
    installing takes 49% of total time.
    """

    def __init__(self, callback: Optional[Callable[[float], None]] = None):
        saved_stdout = os.dup(sys.stdout.fileno())
        saved_stderr = os.dup(sys.stderr.fileno())
        self.stdout = io.TextIOWrapper(os.fdopen(saved_stdout, 'wb'))
        self.stderr = io.TextIOWrapper(os.fdopen(saved_stderr, 'wb'))
        self.last_percent = 0.0
        if callback is None:
            self.callback = lambda p: \
                print(f"{p:.2f}%", flush=True, file=self.stdout)
        else:
            self.callback = callback
        self.update_progress = APTProgressReporter.FetchProgress(
            self.callback, 0, 2, self.stdout, self.stderr)
        self.fetch_progress = APTProgressReporter.FetchProgress(
            self.callback, 2, 51, self.stdout, self.stderr)
        self.upgrade_progress = APTProgressReporter.UpgradeProgress(
            self.callback, 51, 100, self.stdout, self.stderr)

    class _Progress:
        def __init__(
                self,
                callback: Callable[[float], None],
                start, stop,
                stdout: io.TextIOWrapper, stderr: io.TextIOWrapper
        ):
            self.callback = callback
            self.start_percent = start
            self.stop_percent = stop
            self.last_percent = start
            self.stdout = stdout
            self.stderr = stderr

        def notify_callback(self, percent):
            """
            Report ongoing progress.
            """
            _percent = self.start_percent + percent * (
                    self.stop_percent - self.start_percent) / 100
            _percent = round(_percent, 2)
            if self.last_percent < _percent:
                self.callback(_percent)
                self.last_percent = _percent

    class FetchProgress(apt.progress.base.AcquireProgress, _Progress):
        def __init__(
                self,
                callback: Callable[[float], None],
                start, stop,
                stdout: io.TextIOWrapper, stderr: io.TextIOWrapper
        ):
            APTProgressReporter._Progress.__init__(
                self, callback, start, stop, stdout, stderr)

        def fail(self, item):
            """
            Write an error message to the fake stderr.
            """
            print(str(item), flush=True, file=self.stderr)

        def pulse(self, _owner):
            """
            Report ongoing progress on fetching packages.

            Periodically invoked while the Acquire process is underway.
            This function returns a boolean value indicating whether the
            acquisition should be continued (True) or cancelled (False).
            """
            self.notify_callback(self.current_bytes / self.total_bytes * 100)
            return True

        def start(self):
            """Invoked when the Acquire process starts running."""
            super().start()
            self.notify_callback(0)

        def stop(self):
            """Invoked when the Acquire process stops running."""
            super().stop()
            self.notify_callback(100)

    class UpgradeProgress(apt.progress.base.InstallProgress, _Progress):
        def __init__(
                self,
                callback: Callable[[float], None],
                start: int, stop: int,
                stdout: io.TextIOWrapper, stderr: io.TextIOWrapper
        ):
            apt.progress.base.InstallProgress.__init__(self)
            APTProgressReporter._Progress.__init__(
                self, callback, start, stop, stdout, stderr)

        def status_change(self, _pkg, percent, _status):
            """
            Report ongoing progress on installing/upgrading packages.
            """
            self.notify_callback(percent)

        def error(self, pkg, errormsg):
            """
            Write an error message to the fake stderr.
            """
            print("Error during installation " + str(pkg) + ":" + str(errormsg),
                  flush=True, file=self.stderr)

        def start_update(self):
            super().start_update()
            self.notify_callback(0)

        def finish_update(self):
            super().finish_update()
            self.notify_callback(100)
