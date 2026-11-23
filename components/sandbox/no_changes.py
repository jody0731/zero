"""Give bwrap a filter blocking writable opens and external connections.

Run by path before the sandbox hides ZERO's home. libseccomp resolves the
machine's syscall numbers and rejects other architectures. Missing support
fails closed; no command is executed without the filter.
"""

import ctypes
import errno
import os
import sys


ALLOW = 0x7FFF0000
DENY = 0x00050000 | errno.EPERM
BLOCKED = (
    "socket", "connect", "bind", "listen", "accept", "accept4",
    "ptrace", "process_vm_writev", "io_uring_setup",
    "io_uring_enter", "io_uring_register", "creat", "open_by_handle_at",
    "sendmsg", "sendmmsg",
)


class ArgumentComparison(ctypes.Structure):
    _fields_ = [("argument", ctypes.c_uint), ("operation", ctypes.c_int),
                ("mask", ctypes.c_uint64), ("value", ctypes.c_uint64)]


def filter_fd():
    """An inherited descriptor with native BPF, ready for bwrap's --seccomp."""
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ctypes.POINTER(ArgumentComparison),
    ]
    context = library.seccomp_init(ALLOW)
    if not context:
        raise RuntimeError("could not create the no-changes filter")
    descriptor = None
    try:
        for name in BLOCKED:
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number == -1:
                raise RuntimeError(f"no syscall number for {name}")
            result = library.seccomp_rule_add(context, DENY, number, 0)
            if result < 0:
                raise OSError(-result, f"cannot block {name}")
        # A datagram socket pair can send to an external Unix socket without
        # connect(). Only sends to its existing peer are allowed. sendmsg's
        # destination is behind a pointer, so it is blocked whole above.
        comparison = ArgumentComparison(4, 1, 0, 0)
        result = library.seccomp_rule_add_array(
            context, DENY, library.seccomp_syscall_resolve_name(b"sendto"),
            1, ctypes.byref(comparison),
        )
        if result < 0:
            raise OSError(-result, "cannot restrict datagram destinations")
        # Read-only mounts do not stop writes through existing named pipes.
        # Reject writable opens too. The command's output pipes are already
        # open; anonymous socket pairs still serve local event loops.
        for name, argument in (("open", 1), ("openat", 2)):
            number = library.seccomp_syscall_resolve_name(name.encode())
            for flag in (os.O_WRONLY, os.O_RDWR):
                comparison = ArgumentComparison(argument, 7, flag, flag)
                result = library.seccomp_rule_add_array(
                    context, DENY, number, 1, ctypes.byref(comparison),
                )
                if result < 0:
                    raise OSError(-result, f"cannot restrict {name}")
        # Its flags are behind a pointer seccomp cannot inspect. ENOSYS lets
        # software fall back to the checked openat syscall.
        number = library.seccomp_syscall_resolve_name(b"openat2")
        result = library.seccomp_rule_add(
            context, 0x00050000 | errno.ENOSYS, number, 0,
        )
        if result < 0:
            raise OSError(-result, "cannot restrict openat2")
        descriptor = os.memfd_create("no-changes-filter", 0)
        result = library.seccomp_export_bpf(context, descriptor)
        if result < 0:
            raise OSError(-result, "cannot export the no-changes filter")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        library.seccomp_release(context)


def main():
    try:
        descriptor = filter_fd()
    except (OSError, RuntimeError) as error:
        print(f"Read-only sandbox unavailable: {error}", file=sys.stderr)
        return 1
    command = sys.argv[1:]
    os.execv(command[0], [command[0], "--seccomp", str(descriptor), *command[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
