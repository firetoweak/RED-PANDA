"""终端粘贴入口；占位文本与待发送附件一起编辑。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageGrab
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from redpanda.assistant.attachments import AttachmentGateway, AttachmentStore


@dataclass(frozen=True, slots=True)
class ConsoleMessage:
    text: str
    artifact_refs: tuple[str, ...] = ()


def clipboard_text() -> str:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user32.OpenClipboard(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not user32.IsClipboardFormatAvailable(13):
            return ""
        handle = user32.GetClipboardData(13)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


class ImagePaste:
    def __init__(self, gateway: AttachmentGateway) -> None:
        self._gateway = gateway
        self.store: AttachmentStore | None = None
        self.pending: dict[str, str] = {}
        self.number = 0
        self.bindings = KeyBindings()
        self.bindings.add("c-v")(self.paste)
        # Windows Terminal 可能自己处理 Ctrl+V；Alt+V 明确交给应用。
        self.bindings.add("escape", "v")(self.paste)
        self.bindings.add(Keys.BracketedPaste)(self.paste)

    def bind(self, session_id: str) -> None:
        self.store = self._gateway.for_session(session_id)
        self.pending.clear()
        self.number = 0

    def changed(self, buffer) -> None:
        self.pending = {
            token: attachment_id
            for token, attachment_id in self.pending.items()
            if token in buffer.text
        }

    def paste(self, event) -> None:
        image = ImageGrab.grabclipboard()
        if isinstance(image, Image.Image):
            if self.store is None:
                raise RuntimeError("ImagePaste.bind() 必须在粘贴前调用")
            output = BytesIO()
            image.save(output, format="PNG")
            ref = self.store.save_image(output.getvalue(), "image/png")
            self.number += 1
            token = f"[Image #{self.number}]"
            self.pending[token] = ref.attachment_id
            event.current_buffer.insert_text(token)
            return
        text = (
            event.data
            if event.key_sequence[0].key == Keys.BracketedPaste
            else clipboard_text()
        )
        event.current_buffer.insert_text(
            text.replace("\r\n", "\n").replace("\r", "\n")
        )

    def submit(self, text: str) -> ConsoleMessage:
        attached = tuple(
            self.pending[token]
            for token in sorted(self.pending, key=text.index)
            if token in text
        )
        self.pending.clear()
        self.number = 0
        return ConsoleMessage(text.strip(), attached)
