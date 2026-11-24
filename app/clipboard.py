#!/usr/bin/env python3
"""Copy-only clipboard helpers for browser terminals, using OSC 52.

One implementation behind `clip`, `xclip`, `xsel`, `wl-copy` and `wl-paste`.
Reading the clipboard is not supported: a browser terminal can't do it safely.
"""
import base64
import os
import sys

MAX_BYTES = 100_000
READ_MESSAGE = "clipboard read isn't supported in a browser terminal (copy only)"


class UsageError(Exception):
    pass


def osc52(data):
    return b"\x1b]52;c;" + base64.b64encode(data) + b"\x07"


def _split_long(arg):
    name, eq, value = arg.partition("=")
    return name, value if eq else None


def parse_xclip(argv):
    """Return (mode, text_args). xclip reads stdin unless -o is given."""
    mode = "copy"
    value_opts = {"-selection", "-sel", "-display", "-d", "-loops", "-l", "-target", "-t", "-rmlastnl"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-o", "-out"):
            mode = "read"
        elif arg in value_opts:
            i += 1
        i += 1
    return mode, []


def parse_xsel(argv):
    """Return (mode, text_args); mode is copy, read or clear."""
    mode = None
    clear = False
    for arg in argv:
        if arg.startswith("--"):
            name, _ = _split_long(arg)
            if name in ("--output", "-o"):
                mode = "read"
            elif name == "--input":
                mode = "copy"
            elif name == "--clear":
                clear = True
            elif name == "--append":
                mode = mode or "copy"
        elif arg.startswith("-") and len(arg) > 1:
            for flag in arg[1:]:
                if flag == "o":
                    mode = "read"
                elif flag == "i":
                    mode = "copy"
                elif flag == "c":
                    clear = True
                elif flag == "a":
                    mode = mode or "copy"
    if clear and mode != "read":
        return "clear", []
    return mode or "copy", []


def parse_wl_copy(argv):
    """Return (mode, text_args). Text comes from args when given, otherwise stdin."""
    mode = "copy"
    text = []
    rest = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if rest or not arg.startswith("-") or arg == "-":
            text.append(arg)
        elif arg == "--":
            rest = True
        elif arg in ("-c", "--clear"):
            mode = "clear"
        elif arg in ("-t", "--type", "--seat", "-s"):
            i += 1
        i += 1
    return mode, text


def write_tty(seq):
    """Write seq to the controlling terminal; fall back to a stdio tty. False if none."""
    candidates = [os.environ.get("DEVBOX_CLIP_TTY") or "/dev/tty"]
    for fd in (2, 1, 0):
        try:
            if os.isatty(fd):
                candidates.append(os.ttyname(fd))
        except OSError:
            pass
    for path in candidates:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOCTTY)
        except OSError:
            continue
        try:
            os.write(fd, seq)
            return True
        except OSError:
            continue
        finally:
            os.close(fd)
    return False


def main(argv=None, stdin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    flavor = argv.pop(0) if argv else "clip"
    stdin = stdin if stdin is not None else sys.stdin.buffer
    text = []
    if flavor == "wl-paste":
        mode = "read"
    elif flavor == "xclip":
        mode, text = parse_xclip(argv)
    elif flavor == "xsel":
        mode, text = parse_xsel(argv)
    elif flavor == "wl-copy":
        mode, text = parse_wl_copy(argv)
    elif flavor == "clip":
        mode, text = "copy", argv
    else:
        print(f"clipboard: unknown flavor {flavor!r}", file=sys.stderr)
        return 2

    if mode == "read":
        print(f"{flavor}: {READ_MESSAGE}", file=sys.stderr)
        return 1
    if mode == "clear":
        data = b""
    elif text:
        data = " ".join(text).encode()
    else:
        data = stdin.read()
    if len(data) > MAX_BYTES:
        print(f"{flavor}: {len(data)} bytes exceeds the {MAX_BYTES} byte OSC 52 limit", file=sys.stderr)
        return 1
    # No terminal (cron, pipes): nothing can receive the sequence, so it is not an error.
    write_tty(osc52(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
