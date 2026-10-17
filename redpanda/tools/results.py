"""Observable file errors shared by tools and application presentation."""
from redpanda.filesystem.workspace import EnvironmentInputError


def environment_error(exc: EnvironmentInputError) -> dict[str, str | bool]:
    return {"ok": False, "code": exc.code, "error": str(exc)}


def file_error_message(exc: OSError) -> str:
    """Describe an I/O failure without exposing physical execution paths."""
    import errno
    if isinstance(exc, FileNotFoundError):
        return "文件或目录不存在，请确认路径后重试。"
    if isinstance(exc, PermissionError):
        return "文件无法访问，请检查访问权限或是否被其他程序占用。"
    if isinstance(exc, IsADirectoryError):
        return "目标是目录，请指定文件路径。"
    if isinstance(exc, NotADirectoryError):
        return "路径中的某一项不是目录，请检查路径。"
    if exc.errno == errno.ENOSPC:
        return "磁盘空间不足，请释放空间后重试。"
    if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
        return "该文件操作不受支持。"
    return "文件操作未完成，请检查文件占用、访问权限和磁盘状态后重试。"
