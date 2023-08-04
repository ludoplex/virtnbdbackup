"""
    Copyright (C) 2023  Michael Ablassmeier <abi@grinser.de>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import json
import logging
import tempfile
import subprocess
from typing import List
from argparse import Namespace

from libvirtnbdbackup.ssh.exceptions import sshError
from libvirtnbdbackup.processinfo import processInfo
from libvirtnbdbackup.qemu import command
from libvirtnbdbackup.virt.client import DomainDisk

log = logging.getLogger(__name__)


class util:
    """Wrapper for qemu executables"""

    def __init__(self, exportName: str) -> None:
        self.exportName = exportName

    @staticmethod
    def map(cType) -> str:
        """Read extent map using nbdinfo utility"""
        metaOpt = f"--map={cType.metaContext}" if cType.metaContext != "" else "--map"
        cmd = f"nbdinfo --json {metaOpt} '{cType.uri}'"
        log.debug("Starting CMD: [%s]", cmd)
        extentMap = subprocess.run(
            cmd,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        return json.loads(extentMap.stdout)

    @staticmethod
    def create(
        targetFile: str,
        fileSize: int,
        diskFormat: str,
        qcowOptions: list,
        sshClient=None,
    ) -> processInfo:
        """Create the target qcow image"""
        cmd = [
            "qemu-img",
            "create",
            "-f",
            f"{diskFormat}",
            f"{targetFile}",
            "-o",
            f"size={fileSize}",
        ]

        if qcowOptions:
            cmd += qcowOptions

        return command.run(cmd) if not sshClient else sshClient.run(" ".join(cmd))

    def info(self, targetFile: str, sshClient=None) -> processInfo:
        """Return qemu image information"""
        cmd = ["qemu-img", "info", f"{targetFile}", "--output", "json", "--force-share"]
        if not sshClient:
            return command.run(cmd, toPipe=True)

        return sshClient.run(" ".join(cmd))

    def startRestoreNbdServer(self, targetFile: str, socketFile: str) -> processInfo:
        """Start local nbd server process for restore operation"""
        cmd = [
            "qemu-nbd",
            "--discard=unmap",
            "--format=qcow2",
            "-x",
            f"{self.exportName}",
            f"{targetFile}",
            "-k",
            f"{socketFile}",
            "--fork",
        ]
        return command.run(cmd)

    @staticmethod
    def _gt(prefix: str, suffix: str, delete: bool = False) -> str:
        """Create named temporary file."""
        with tempfile.NamedTemporaryFile(
            delete=delete, prefix=prefix, suffix=suffix
        ) as tf1:
            return tf1.name

    @staticmethod
    def _addTls(cmd: List[str], certpath: str) -> None:
        """Add required tls related options to qemu-nbd command
        line."""
        cmd.extend(
            (
                "--object",
                f"tls-creds-x509,id=tls0,endpoint=server,dir={certpath},verify-peer=false",
                "--tls-creds tls0",
            )
        )

    def startRemoteRestoreNbdServer(
        self, args: Namespace, targetFile: str
    ) -> processInfo:
        """Start nbd server process remotely over ssh for restore operation"""
        pidFile = self._gt("qemu-nbd-restore", ".pid")
        logFile = self._gt("qemu-nbd-restore", ".log")
        cmd = [
            "qemu-nbd",
            "--discard=unmap",
            "--format=qcow2",
            "-x",
            f"{self.exportName}",
            f"{targetFile}",
            "-p",
            f"{args.nbd_port}",
            "--pid-file",
            f"{pidFile}",
            "--fork",
        ]
        if args.tls is True:
            self._addTls(cmd, args.tls_cert)
        cmd.append(f"> {logFile} 2>&1")
        try:
            return args.sshClient.run(" ".join(cmd), pidFile, logFile)
        except sshError:
            log.error("Executing command failed: check [%s] for errors.", logFile)
            raise

    def startNbdkitProcess(
        self, args: Namespace, nbdkitModule: str, blockMap, fullImage: str
    ) -> processInfo:
        """Execute nbdkit process for virtnbdmap"""
        pidFile = self._gt("nbdkit", ".pid")
        debug = "1" if args.verbose else "0"
        cmd = [
            "nbdkit",
            "--pidfile",
            f"{pidFile}",
            "-i",
            f"{args.listen_address}",
            "-p",
            f"{args.listen_port}",
            "-e",
            f"{self.exportName}",
            "--filter=blocksize",
            "--filter=cow",
            "-v",
            "python",
            f"{nbdkitModule}",
            f"maxlen={args.blocksize}",
            f"blockmap={blockMap}",
            f"disk={fullImage}",
            f"debug={debug}",
            "-t",
            f"{args.threads}",
        ]
        return command.run(cmd, pidFile=pidFile)

    def startBackupNbdServer(
        self, diskFormat: str, diskFile: str, socketFile: str, bitMap: str
    ) -> processInfo:
        """Start nbd server process for offline backup operation"""
        bitmapOpt = f"--bitmap={bitMap}" if bitMap != "" else "--"
        pidFile = f"{socketFile}.pid"
        cmd = [
            "qemu-nbd",
            "-r",
            f"--format={diskFormat}",
            "-x",
            f"{self.exportName}",
            f"{diskFile}",
            "-k",
            f"{socketFile}",
            "-t",
            "-e 2",
            "--fork",
            "--detect-zeroes=on",
            f"--pid-file={pidFile}",
            bitmapOpt,
        ]
        return command.run(cmd, pidFile=pidFile)

    def startRemoteBackupNbdServer(
        self, args: Namespace, disk: DomainDisk, bitMap: str, port: int
    ) -> processInfo:
        """Start nbd server process remotely over ssh for restore operation"""
        pidFile = self._gt("qemu-nbd-backup", ".pid")
        logFile = self._gt("qemu-nbd-backup", ".log")
        cmd = [
            "qemu-nbd",
            "-r",
            f"--format={disk.format}",
            "-x",
            f"{self.exportName}",
            f"{disk.path}",
            "-p",
            f"{port}",
            "--pid-file",
            f"{pidFile}",
            "--fork",
        ]
        if args.nbd_ip != "":
            cmd.extend(("-b", args.nbd_ip))
        if bitMap != "":
            cmd.append(f"--bitmap={bitMap}")

        if args.tls is True:
            self._addTls(cmd, args.tls_cert)
        cmd.append(f"> {logFile} 2>&1")
        try:
            return args.sshClient.run(" ".join(cmd), pidFile, logFile)
        except sshError:
            log.error("Executing command failed: check [%s] for errors.", logFile)
            raise

    def disconnect(self, device: str) -> processInfo:
        """Disconnect device"""
        log.info("Disconnecting device [%s]", device)
        cmd = ["qemu-nbd", "-d", f"{device}"]
        return command.run(cmd)
