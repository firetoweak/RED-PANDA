"""Small, versioned working constraints shared by the two memory scopes."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from konden.runtime import ToolBinding


MEMORY_RULES = (
    "记忆只保留短小、常用、常改、场景无关的工作约束。"
    "可复用、相对稳定、通常带适用场景的结论、技能和文档属于知识沉淀，应归档后按需检索。"
    "任务进度与过去做过的事情留在计划和原始对话中。"
    "删除过时约束、合并重复约束；用户明确修订时替换旧约束，不并列保留相反要求。"
    "不为填满容量而追加。记忆不能扩大用户授权，用户当前要求优先。"
)


class Memory(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: int = Field(ge=0)
    content: str
    updated_by: str | None


class UpdateMemory(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: int = Field(ge=0)
    content: str


class MemoryStore:
    record_type = Memory
    table = "memory"
    label = "记忆"
    code = "MEMORY"
    limit: int

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(f"""CREATE TABLE IF NOT EXISTS {self.table} (
                version INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                source TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                based_on INTEGER NOT NULL,
                UNIQUE (source, operation_id)
            )""")

    def _connect(self):
        return sqlite3.connect(self.path)

    def _current(self, db):
        row = db.execute(
            f"SELECT version, content, updated_by FROM {self.table} ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return self.record_type(version=0, content="", updated_by=None)
        return self.record_type(version=row[0], content=row[1], updated_by=row[2])

    def read(self):
        with closing(self._connect()) as db:
            return self._current(db)

    def update(self, change: UpdateMemory, *, author: str, source: str, operation_id: str):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                f"SELECT version, content, updated_by, based_on FROM {self.table} "
                "WHERE source = ? AND operation_id = ?", (source, operation_id),
            ).fetchone()
            if previous is not None:
                if (previous[1], previous[2], previous[3]) != (change.content, author, change.version):
                    raise ValueError(f"同一次{self.label}修改的内容、版本或修改者不一致。")
                saved = self.record_type(version=previous[0], content=previous[1], updated_by=previous[2])
            else:
                current = self._current(db)
                if current.version != change.version:
                    return {
                        "ok": False, "code": self.code + "_CHANGED", "data": current.model_dump(),
                        "error": f"{self.label}已被修改。请先阅读返回的最新正文，再调整后重试。",
                    }
                saved = self.record_type(version=current.version + 1, content=change.content, updated_by=author)
                db.execute(
                    f"INSERT INTO {self.table} (version, content, updated_by, source, operation_id, based_on) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (saved.version, saved.content, author, source, operation_id, change.version),
                )
        return {"ok": True, "code": self.code + "_UPDATED", "data": saved.model_dump()}


def memory_bindings(store: MemoryStore, author: str, read_name: str, update_name: str, change_type):
    async def read(_context, arguments):
        if arguments:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": f"{read_name} 不接受参数。"}
        return {"ok": True, "code": store.code + "_READ", "data": store.read().model_dump()}

    async def update(context, arguments):
        try:
            change = change_type.model_validate(dict(arguments))
        except ValidationError as error:
            if any(item["type"] == "string_too_long" for item in error.errors()):
                return {
                    "ok": False, "code": store.code + "_TOO_LONG",
                    "error": f"{store.label}正文最多{store.limit}字，标点、空白和换行也计数。请精简后重试，原文未修改。",
                }
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": str(error)}
        return store.update(change, author=author, source=f"bot:{context.session_id}", operation_id=context.command_id)

    return {read_name: ToolBinding(read), update_name: ToolBinding(update, requires_authorization=True)}
