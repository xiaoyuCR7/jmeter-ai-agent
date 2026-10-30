"""Agent tools for filesystem operations — read/write files, list directories."""

import os
import json
import asyncio

from app.core.config import settings
from app.services.ai.agent.tool_registry import Tool

def _resolve_path(path: str, *, writable: bool = False, allow_root_listing: bool = False) -> str:
    """Resolve and validate a path is within allowed directories.

    Returns the absolute path if valid, otherwise raises ValueError.
    """
    # If not absolute, resolve relative to TEST_PLANS_DIR
    if not os.path.isabs(path):
        path = os.path.join(settings.TEST_PLANS_DIR, path)
    abs_path = os.path.realpath(os.path.abspath(path))
    roots = [settings.TEST_PLANS_DIR, os.path.join(settings.BASE_DIR, 'test_data')]
    if not writable:
        roots.extend([settings.REPORTS_DIR, settings.BACKUPS_DIR])

    # Check path is within allowed roots
    for root in roots:
        root = os.path.realpath(os.path.abspath(root))
        try:
            if os.path.normcase(os.path.commonpath([abs_path, root])) == os.path.normcase(root):
                return abs_path
        except ValueError:
            continue

    raise ValueError(
        f"Access denied: path '{path}' is outside allowed directories. "
        f"Writes are limited to test_plans and test_data; reports and backups are read-only."
    )


async def read_file(path: str, encoding: str = "utf-8", max_lines: int = 500) -> dict:
    """Read a file from disk.

    Args:
        path: Absolute or relative path to the file.
        encoding: File encoding (default utf-8).
        max_lines: Maximum number of lines to return.
    """
    if not isinstance(max_lines, int) or not 1 <= max_lines <= 5000:
        return {'error': 'max_lines must be within [1, 5000]'}
    try:
        safe_path = _resolve_path(path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(safe_path):
        return {"error": f"File not found: {path}"}

    if not os.path.isfile(safe_path):
        return {"error": f"Not a file: {path}"}

    try:
        def read_bounded():
            lines, size = [], 0
            with open(safe_path, 'r', encoding=encoding, errors='replace') as stream:
                while len(lines) < max_lines and size < 65536:
                    line = stream.readline(65536 - size)
                    if not line:
                        break
                    lines.append(line)
                    size += len(line)
                truncated = bool(stream.read(1))
            return lines, truncated
        lines, truncated = await asyncio.to_thread(read_bounded)

        return {
            "path": safe_path,
            "content": "".join(lines),
            "total_lines": None if truncated else len(lines),
            "returned_lines": len(lines),
            "truncated": truncated,
            "size_bytes": os.path.getsize(safe_path),
        }
    except Exception as e:
        return {"error": str(e)}


async def write_file(path: str, content: str, encoding: str = "utf-8") -> dict:
    """Write content to a file. Creates directories if needed.

    Args:
        path: Absolute or relative path to the file.
        content: Content to write.
        encoding: File encoding (default utf-8).
    """
    try:
        safe_path = _resolve_path(path, writable=True)
    except ValueError as e:
        return {"error": str(e)}

    try:
        dir_name = os.path.dirname(safe_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        with open(safe_path, "w", encoding=encoding) as f:
            f.write(content)

        return {
            "path": safe_path,
            "written": True,
            "size_bytes": os.path.getsize(safe_path),
            "message": f"File written: {safe_path}",
        }
    except Exception as e:
        return {"error": str(e)}


async def list_directory(directory: str, pattern: str = "*") -> dict:
    """List files and subdirectories in a directory.

    Args:
        directory: Directory path to list.
        pattern: Optional glob pattern filter (e.g., "*.jmx").
    """
    try:
        safe_dir = _resolve_path(directory, allow_root_listing=True)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(safe_dir):
        return {"error": f"Directory not found: {directory}"}

    if not os.path.isdir(safe_dir):
        return {"error": f"Not a directory: {directory}"}

    import fnmatch

    items = []
    try:
        for entry in os.listdir(safe_dir):
            entry_path = os.path.join(safe_dir, entry)
            if pattern == "*" or fnmatch.fnmatch(entry, pattern):
                items.append({
                    "name": entry,
                    "path": entry_path,
                    "type": "directory" if os.path.isdir(entry_path) else "file",
                    "size_bytes": os.path.getsize(entry_path) if os.path.isfile(entry_path) else None,
                })
    except PermissionError as e:
        return {"error": f"Permission denied: {e}"}

    return {
        "directory": safe_dir,
        "pattern": pattern,
        "items": sorted(items, key=lambda x: (x["type"], x["name"])),
        "count": len(items),
    }


async def delete_file(path: str) -> dict:
    """Delete a file.

    Args:
        path: Path to the file to delete.

    Requires confirmation for safety.
    """
    try:
        safe_path = _resolve_path(path, writable=True)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(safe_path):
        return {"error": f"File not found: {path}"}

    if not os.path.isfile(safe_path):
        return {"error": f"Not a file: {path}"}

    try:
        os.remove(safe_path)
        return {"deleted": True, "path": safe_path, "message": f"Deleted: {safe_path}"}
    except Exception as e:
        return {"error": str(e)}


async def file_exists(path: str) -> dict:
    """Check if a file or directory exists.

    Args:
        path: Path to check.
    """
    try:
        safe_path = _resolve_path(path)
    except ValueError:
        return {"path": path, "exists": False, "is_file": False, "is_dir": False}

    return {
        "path": safe_path,
        "exists": os.path.exists(safe_path),
        "is_file": os.path.isfile(safe_path),
        "is_dir": os.path.isdir(safe_path),
    }


# Tool definitions
file_tools = [
    Tool(
        name="read_file",
        description="Read a file from disk. Returns content with line count and size info.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "encoding": {"type": "string", "description": "File encoding (default utf-8)"},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 5000,
                              "description": "Max lines to return (default 500); output also limited to 65536 characters"},
            },
            "required": ["path"],
        },
        handler=read_file,
    ),
    Tool(
        name="write_file",
        description="Write content to a file. Creates parent directories if needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "content": {"type": "string", "description": "Content to write"},
                "encoding": {"type": "string", "description": "File encoding (default utf-8)"},
            },
            "required": ["path", "content"],
        },
        handler=write_file,
        risk='write',
    ),
    Tool(
        name="list_directory",
        description="List files and subdirectories in a directory with optional pattern filter.",
        parameters={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Directory path to list"},
                "pattern": {"type": "string", "description": "Glob pattern filter (e.g. *.jmx, *.jtl)"},
            },
            "required": ["directory"],
        },
        handler=list_directory,
    ),
    Tool(
        name="delete_file",
        description="Delete a file. Use with caution.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to delete"},
            },
            "required": ["path"],
        },
        handler=delete_file,
        risk='write',
    ),
    Tool(
        name="file_exists",
        description="Check if a file or directory exists on disk.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to check"},
            },
            "required": ["path"],
        },
        handler=file_exists,
    ),
]
