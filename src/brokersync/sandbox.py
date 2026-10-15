"""Run a foreign program (Scalable's ``sc``) so it can't read the service's data or key (#93).

``python -I -m brokersync.sandbox --rw DIR --ro DIR … -- PROGRAM ARGS…`` restricts itself with Landlock
(Linux ≥ 5.13, no privileges needed, also in an unprivileged container) and then executes the program:
it may read the system (``/usr``, ``/etc``, ``/proc`` …) and the listed ``--ro`` paths, and write only the
``--rw`` paths. ``data/``, ``/run/credentials`` (the key) and the unlock cache in ``/run`` are not among
them (the service's environment holds no secret, only the path of the key). The network stays open - ``sc``
needs it.

A kernel without Landlock runs the program without it; the page *Sicherheit* says so.
"""

from __future__ import annotations

import ctypes
import os
import sys

SYSTEM = ("/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc", "/proc", "/sys", "/dev")

# Syscall numbers are the same on x86_64 and arm64 (asm-generic).
_CREATE, _ADD_RULE, _RESTRICT = 444, 445, 446
_VERSION = 1  # LANDLOCK_CREATE_RULESET_VERSION
_PATH_BENEATH = 1
_NO_NEW_PRIVS = 38  # PR_SET_NO_NEW_PRIVS

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1, 2, 4, 8
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV
READ = EXECUTE | READ_FILE | READ_DIR


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def supported() -> int:
    """The kernel's Landlock ABI version, 0 without Landlock."""
    try:
        version = _libc().syscall(_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(_VERSION))
    except (OSError, AttributeError):
        return 0
    return max(int(version), 0)


def _handled(abi: int) -> int:
    rights = (1 << 13) - 1  # ABI 1: execute … make_sym
    if abi >= 2:
        rights |= REFER
    if abi >= 3:
        rights |= TRUNCATE
    if abi >= 5:
        rights |= IOCTL_DEV
    return rights


def restrict(read: list[str], write: list[str]) -> bool:
    """Landlock this process: read (and execute) ``read`` and the system, write only ``write``. False without
    Landlock; raises OSError if Landlock is there but fails."""
    abi = supported()
    if not abi:
        return False
    libc = _libc()
    handled = _handled(abi)
    attr = ctypes.c_uint64(handled)  # struct landlock_ruleset_attr, only handled_access_fs: the net stays open
    ruleset = libc.syscall(_CREATE, ctypes.byref(attr), ctypes.c_size_t(8), ctypes.c_uint32(0))
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        rules = [(p, READ | (WRITE_FILE if p == "/dev" else 0)) for p in SYSTEM] + [(p, READ) for p in read]
        rules += [(p, handled) for p in write]
        for path, rights in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            try:
                if not os.path.isdir(path):
                    rights &= FILE_RIGHTS
                rule = _PathBeneath(rights & handled, fd)
                if libc.syscall(_ADD_RULE, ruleset, _PATH_BENEATH, ctypes.byref(rule), 0) < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
            finally:
                os.close(fd)
        if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
        if libc.syscall(_RESTRICT, ruleset, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(ruleset)
    return True


def command(program: str, args: list[str], *, read: list[str], write: list[str]) -> list[str]:
    """The command line that runs ``program`` in the sandbox."""
    paths = [a for p in read for a in ("--ro", p)] + [a for p in write for a in ("--rw", p)]
    return [sys.executable, "-I", "-m", "brokersync.sandbox", *paths, "--", program, *args]


def main(argv: list[str]) -> None:
    read: list[str] = []
    write: list[str] = []
    while argv and argv[0] != "--":
        flag, path, *argv = argv
        if flag not in ("--ro", "--rw"):
            raise SystemExit(f"unknown option {flag}")
        (read if flag == "--ro" else write).append(path)
    program, *args = argv[1:]
    try:
        restrict(read, write)
    except OSError as e:  # a kernel or seccomp profile that has Landlock but refuses it: run, and say so
        print(f"brokersync.sandbox: Landlock failed ({e}); running without", file=sys.stderr)
    os.execv(program, [program, *args])


if __name__ == "__main__":
    main(sys.argv[1:])
