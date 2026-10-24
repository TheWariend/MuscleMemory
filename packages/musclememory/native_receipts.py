"""Read bounded native Codex receipts, never command output posing as a receipt."""

import json
import os
import shlex
import stat
from urllib.parse import unquote, urlsplit

from .artifacts import command_parts

MAX_RECEIPT_BYTES = 4 * 1024 * 1024


def local_directory(value):
    """Desktop native items use local file URLs; hooks use filesystem paths."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("file:"):
        url = urlsplit(value)
        if url.netloc not in {"", "localhost"} or url.query or url.fragment:
            return None
        value = unquote(url.path)
    return os.path.realpath(value) if os.path.isabs(value) and "\0" not in value else None


def completed_tools(path, turn, session=None):
    """Completed items in this exact turn, keyed by the native tool call ID.

    Only the native event envelope is accepted. Text printed by a command, MCP
    output, and receipts from another turn cannot attest a command's exit code.
    A large transcript is read from the tail without loading the entire chat.
    """
    if not isinstance(path, str) or not path or not isinstance(turn, str) or not turn:
        return {}
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return {}
            offset = max(0, info.st_size - MAX_RECEIPT_BYTES)
            stream.seek(offset)
            data = stream.read(MAX_RECEIPT_BYTES)
        lines = data.splitlines()
        if offset:
            lines = lines[1:]  # discard a potentially partial JSON line
    except (OSError, ValueError):
        return {}
    result, conflicts = {}, set()
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        event = record.get("payload")
        if not isinstance(event, dict) or event.get("type") != "item_completed":
            continue
        if event.get("turn_id") != turn:
            continue
        if session is not None and event.get("thread_id") != session:
            continue
        item = event.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        source = item["id"]
        if source in result and result[source] != item:
            conflicts.add(source)
        result[source] = item
    return {source: item for source, item in result.items() if source not in conflicts}


def command_receipt(item, arguments, cwd):
    """Match an execution receipt to its command and directory, in addition to ID."""
    if not isinstance(item, dict) or item.get("type") != "CommandExecution":
        return None
    if item.get("status") not in {"completed", "failed"} or type(item.get("exit_code")) is not int:
        return None
    argv = item.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) for word in argv):
        return None
    command = arguments.get("command") if isinstance(arguments, dict) else None
    expected, actual = command_parts(command), command_parts(shlex.join(argv))
    if not expected or expected != actual:
        return None
    directory = local_directory(item.get("cwd"))
    cwd = local_directory(arguments.get("workdir", arguments.get("cwd", cwd)))
    if not cwd or not directory:
        return None
    try:
        if os.path.realpath(cwd) != os.path.realpath(directory):
            return None
    except (OSError, ValueError):
        return None
    return item
