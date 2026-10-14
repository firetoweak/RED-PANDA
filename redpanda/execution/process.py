"""Raw argv task execution for the current platform."""
import os

if os.name == "nt":
    from .process_win import Running
else:
    from .process_posix import Running
