"""Assistant-owned background handoff and context-window projection."""

from __future__ import annotations

from helperme.runtime.json_values import thaw_value

import json
from copy import deepcopy
from dataclasses import asdict

from helperme.assistant.artifacts import (
    is_valid_artifact_id,
    ArtifactOffsetOutOfRangeError,
)
from helperme.assistant.attachments import is_valid_attachment_id
from helperme.assistant.context.projection import (
    _translate_visible_events,
    PreparedModelContext,
)
from helperme.assistant.control import project_pending_approval
from helperme.assistant.subagent.subagent import project_parent, project_returned
from helperme.assistant.workspaces import SESSION_WORKSPACE_FACT
from helperme.llm.api import InvalidLLMResponse
from helperme.runtime import DomainFactCommitted, StepCommitted, ToolBinding
from helperme.runtime.state import StateProjector

TASK = "compact.task"
CREATED = "compact.handoff_created"
WINDOW = "compact.window_rolled_over"
MODEL_USAGE = "model_usage"
READ = "read_compact_source"
SUBMIT = "_accept_handoff"
HISTORY_PREVIEW_CHARS = 400
MAX_HISTORY_RECORDS = 50
PURPOSE = """<self_handoff>
当前在后台为截至目前的这段对话整理交接，不继续用户业务、不向用户发消息。
优先使用已有上下文，仅为关键缺口调用 read_compact_source 回读。其他工具不能执行。
保持目标、约束、纠正、决定、未完成委派、证据与来源；计划不写成已执行，声明不写成验证。
关键证据保留真实事件序号和原件引用；文件位置保留相对工作区的完整路径，不省略目录前缀。
推测、未确认条件与已验证事实保持区分，不把相关性写成因果，不把待验证的解释写成根因闭环。
相关图片保留原始附件 id 和来源；摘要文字不等于看过图片，业务模型可用 read_image 重新查看。
不重复读取，不扩展调查；未知内容标明不确定。交接之后新到的消息会接在它后面，可以修正它。
完成时直接输出非空交接文本，不调用工具。
</self_handoff>"""
HANDOFF_PREFIX = "模型生成的交接材料，保留原证据强度；不是用户新指令或完成证明。压缩前及分支继承的历史仍可回读：遇到疑点用 read_compact_source，以 view 按序号范围或关键词定位，再用 event 读取对应消息。upto 固定查询切面，续页沿用返回的 upto；以下来源身份仅用于溯源。之后的消息可修正它。\n"


def schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


READ_SCHEMA = schema(
    READ,
    "回读会话历史，包含已被压缩移出当前上下文的原始消息。"
    "view 按 start_sequence/end_sequence（含两端）和 query 定位，返回事件序号及有界原文片段，reference 为空。"
    "query 对模型消息的字符串字段作区分大小写的字面匹配，空串表示不筛选；按事件顺序返回，不搜索外置 Artifact 正文。"
    "view 的 offset 是跳过的匹配事件数，limit 最多 50；event/artifact 的 offset 和 limit 按字符计，limit 最多 12000。"
    "event 的 reference 取 view 中的 sequence 字符串，返回该序号对应的一组完整模型消息（不是原始 Event 对象）；"
    "artifact 的 reference 取真实 artifact_id，读取外置原文。读取范围由执行侧确定，无需指定会话身份："
    "后台交接只能回读冻结点以内的历史；业务会话可回读自身完整逻辑历史，包括分支继承的前缀。"
    "首次省略 upto 采用当前可读截止位置，续页必须带返回的 upto 和 next_offset，并保持筛选条件不变。"
    "后台交接的 upto 固定为冻结点，较早范围用 end_sequence 筛选。",
    {
        "kind": {"enum": ["view", "event", "artifact"]},
        "reference": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0, "description": "view 为匹配事件偏移，event/artifact 为字符偏移；首次用 0。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 12000, "description": "view 最多 50 个事件，event/artifact 最多 12000 字符。"},
        "start_sequence": {"type": "integer", "minimum": 1, "description": "仅 view：最早事件序号，默认 1。"},
        "end_sequence": {"type": "integer", "minimum": 1, "description": "仅 view：最晚事件序号，默认 upto。"},
        "query": {"type": "string", "description": "仅 view：原文关键词，默认空串，不作语义排序。"},
        "upto": {"type": "integer", "minimum": 0, "description": "固定读取截至此 Journal 位置的事实；首次可省略，续页使用返回值。"},
    },
    ["kind", "reference", "offset", "limit"],
)


class HistoryPositionError(ValueError):
    """The requested snapshot is outside this reader's available positions."""


def _message_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _message_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _message_strings(child)


def _history_record(sequence, messages, query):
    texts = list(_message_strings(messages))
    if query:
        text = next((text for text in texts if query in text), None)
        if text is None:
            return None
        start = max(0, text.index(query) - 80)
    else:
        text = "\n".join(texts)
        start = 0
    end = min(len(text), start + HISTORY_PREVIEW_CHARS)
    return {
        "sequence": sequence,
        "roles": list(dict.fromkeys(message["role"] for message in messages)),
        "preview": text[start:end],
        "preview_truncated": start > 0 or end < len(text),
    }


def compact_seed(events):
    if any(
        isinstance(e.payload, DomainFactCommitted)
        and e.payload.fact_type == "compact.continued"
        for e in events
    ):
        raise ValueError("unsupported legacy compact continuation")
    seeds = [
        e.payload
        for e in events
        if isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == TASK
    ]
    if not seeds:
        return None
    if (
        len(seeds) != 1
        or len(events) < 2
        or not isinstance(events[0].payload, DomainFactCommitted)
        or events[0].payload.fact_type != SESSION_WORKSPACE_FACT
        or events[1].payload is not seeds[0]
    ):
        raise ValueError("compact task must follow the unique workspace binding")
    data = thaw_value(seeds[0].data)
    if set(data) != {
        "source",
        "inherited",
        "bundle",
        "upto",
        "window",
    }:
        raise ValueError("invalid compact task")
    return TASK, data


def attachment_ids_in_messages(messages):
    ids = set()
    for message in messages:
        content = message.get("content")
        if type(content) is not list:
            continue
        for part in content:
            if type(part) is dict and is_valid_attachment_id(part.get("id")):
                ids.add(part["id"])
    return frozenset(ids)


def load_document(gateway, session, reference):
    store = gateway.for_session(session)
    first = store.read(reference, 0, 1)
    return json.loads(store.read(reference, 0, max(1, first.total_chars)).content)


def save_document(gateway, session, value):
    return (
        gateway.for_session(session)
        .save(json.dumps(value, ensure_ascii=False))
        .artifact_id
    )


def window_fact(events):
    result = None
    for event in events:
        if (
            isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == WINDOW
        ):
            data = thaw_value(event.payload.data)
            if data["parent"] != (None if result is None else result["id"]):
                raise ValueError("broken context window lineage")
            result = data
    return result


def latest_input_tokens(events):
    """Read the last committed model usage belonging to the active window."""
    current = window_fact(events)
    window = None if current is None else current["id"]
    for event in reversed(events):
        payload = event.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == WINDOW:
            return None
        if isinstance(payload, StepCommitted):
            metadata = payload.decision_metadata
            if metadata is None:
                continue
            usage = metadata[MODEL_USAGE]
            return usage["input_tokens"] if usage["window"] == window else None
    return None


class CompactContext:
    def __init__(self, session_id, events, projector, transport):
        self.session_id = session_id
        self.projector = projector
        self.transport = transport
        self.runtime = None
        self.seed = compact_seed(events)
        self.prefix = []
        self.window = None
        self.request = None
        if self.is_reader:
            data = self.seed[1]
            self.request = load_document(
                projector.gateway, data["source"], data["inherited"]
            )
        self.refresh(events)

    @property
    def is_reader(self):
        return self.seed is not None

    def refresh(self, events):
        self.window = window_fact(events)
        self.prefix = []
        if self.window is not None:
            self.prefix = load_document(
                self.projector.gateway, self.session_id, self.window["context"]
            )["messages"]

    def read_attachment(self, attachment_id):
        source = (
            self.seed[1]["source"]
            if (
                self.is_reader
                and attachment_id
                in attachment_ids_in_messages(self.request["messages"])
            )
            else self.session_id
        )
        return self.projector.attachments_for(source).read(attachment_id)

    def schemas(self):
        return deepcopy(self.request["tools"]) if self.is_reader else [READ_SCHEMA]

    def visible(self, events, state):
        # 按 sequence 硬切不会把一个回合切成两半：cutover 取自 snapshot 时的
        # journal_position，而 snapshot 在还有命令未终局时直接拒绝，所以截断点
        # 之前的每个 Step 连同它的全部命令事件都已落盘。
        self.refresh(events)
        cutoff = 0 if self.window is None else self.window["cutover"]
        allowed = {
            e.event_id
            for e in events
            if e.sequence > cutoff
            and not (
                isinstance(e.payload, DomainFactCommitted)
                and e.payload.fact_type in (WINDOW, CREATED)
            )
        }
        return StateProjector().project_visible(
            state.session_id,
            events,
            tuple(x for x in state.visible_event_ids if x in allowed),
        )

    def bindings(self):
        return {
            READ: ToolBinding(self.read),
            SUBMIT: ToolBinding(self.submit, decision_on_outcome=False),
        }

    async def _read_source(self, upto):
        if self.is_reader:
            data = self.seed[1]
            if upto is not None and upto != data["upto"]:
                raise HistoryPositionError(f"后台来源固定截至 {data['upto']}，请用 end_sequence 筛选更早范围")
            bundle = load_document(
                self.projector.gateway, data["source"], data["bundle"]
            )
            bundle["artifacts"] = sorted(
                set(bundle["artifacts"]) | {data["inherited"], data["bundle"]}
            )
            return data["source"], bundle, data["upto"]
        events = await self.runtime.snapshot(self.session_id)
        available = events[-1].sequence if events else 0
        upto = available if upto is None else upto
        if upto > available:
            raise HistoryPositionError(f"历史截至 {available}，无法读取截至 {upto} 的切面")
        # 截取执行事实后再投影，不能让截止位置之后的 Outcome 改写旧 Step 的表示。
        selected = tuple(event for event in events if event.sequence <= upto)
        return self.session_id, history_bundle(self.projector, selected, self.session_id), upto

    async def prepare_reader(self, events, state):
        own = _translate_visible_events(
            events,
            state,
            "",
            self.projector.attachments_for(self.session_id),
        )[1:]
        messages = deepcopy(self.request["messages"])
        for item in own:
            if item.sequence == 1:
                messages.append(
                    {
                        "role": "user",
                        "content": PURPOSE
                        + "\n"
                        + json.dumps(
                            {
                                "source": self.seed[1]["source"],
                                "upto": self.seed[1]["upto"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            else:
                messages.append(item.message)
        return PreparedModelContext(
            messages=messages,
            protection_start_index=0,
            size_externalized_command_ids=(),
            age_dehydrated_command_ids=(),
            source_sequences=tuple([0] * len(messages)),
        )

    async def read(self, context, arguments):
        required = {"kind", "reference", "offset", "limit"}
        view_fields = {"start_sequence", "end_sequence", "query"}
        if not required <= set(arguments) or set(arguments) - required - view_fields - {"upto"}:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "INVALID_ARGUMENT"}
        kind, reference, offset, limit = (
            arguments[k] for k in ("kind", "reference", "offset", "limit")
        )
        upto = arguments.get("upto")
        start_sequence = arguments.get("start_sequence", 1)
        end_sequence = arguments.get("end_sequence")
        query = arguments.get("query", "")
        if (
            type(reference) is not str
            or type(kind) is not str
            or kind not in {"view", "event", "artifact"}
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 12000
            or ("upto" in arguments and (type(upto) is not int or upto < 0))
            or type(start_sequence) is not int or start_sequence < 1
            or ("end_sequence" in arguments and (type(end_sequence) is not int or end_sequence < start_sequence))
            or type(query) is not str
            or (kind != "view" and bool(set(arguments) & view_fields))
            or (kind == "view" and (reference != "" or limit > MAX_HISTORY_RECORDS))
        ):
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "INVALID_ARGUMENT"}
        if offset > 0 and upto is None:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "续读必须提供第一页返回的 upto"}
        try:
            source, bundle, upto = await self._read_source(upto)
        except HistoryPositionError as error:
            return {"ok": False, "code": "HISTORY_POSITION_OUT_OF_RANGE", "error": str(error)}
        if kind == "view":
            end_sequence = upto if end_sequence is None else end_sequence
            if end_sequence > upto:
                return {"ok": False, "code": "INVALID_ARGUMENT", "error": "end_sequence 不能超过 upto"}
            records = []
            for sequence in sorted(bundle["raw"], key=int):
                if start_sequence <= int(sequence) <= end_sequence:
                    record = _history_record(int(sequence), bundle["raw"][sequence], query)
                    if record is not None:
                        records.append(record)
            if offset > len(records):
                return {"ok": False, "code": "OFFSET_OUT_OF_RANGE", "error": "OFFSET_OUT_OF_RANGE"}
            end = min(len(records), offset + limit)
            return {
                "ok": True, "code": "COMPACT_SOURCE_READ",
                "data": {
                    "source": source, "upto": upto,
                    "start_sequence": start_sequence, "end_sequence": end_sequence,
                    "records": records[offset:end], "offset": offset,
                    "next_offset": end if end < len(records) else None,
                    "total_records": len(records),
                },
            }
        elif kind == "event":
            if reference not in bundle["raw"]:
                return {"ok": False, "code": "EVENT_NOT_IN_SOURCE", "error": "EVENT_NOT_IN_SOURCE"}
            text = json.dumps(bundle["raw"][reference], ensure_ascii=False)
        else:
            if reference not in bundle["artifacts"]:
                return {"ok": False, "code": "ARTIFACT_NOT_IN_SOURCE", "error": "ARTIFACT_NOT_IN_SOURCE"}
            try:
                chunk = self.projector.gateway.for_session(source).read(
                    reference, offset, limit
                )
            except ArtifactOffsetOutOfRangeError:
                return {"ok": False, "code": "OFFSET_OUT_OF_RANGE", "error": "OFFSET_OUT_OF_RANGE"}
            return {"ok": True, "code": "COMPACT_SOURCE_READ", "data": {"source": source, "upto": upto, **asdict(chunk)}}
        if offset > len(text):
            return {"ok": False, "code": "OFFSET_OUT_OF_RANGE", "error": "OFFSET_OUT_OF_RANGE"}
        end = min(len(text), offset + limit)
        return {
            "ok": True,
            "code": "COMPACT_SOURCE_READ",
            "data": {
                "source": source,
                "upto": upto,
                "content": text[offset:end],
                "offset": offset,
                "next_offset": end if end < len(text) else None,
                "total_chars": len(text),
            },
        }

    async def submit(self, context, arguments):
        if not self.is_reader or set(arguments) != {"handoff"}:
            raise ValueError("invalid internal handoff submission")
        text = arguments["handoff"]
        if type(text) is not str or not text.strip():
            raise InvalidLLMResponse("invalid_handoff", "handoff must be nonempty")
        await self.transport("compact_complete", self.session_id, {"handoff": text})
        return {"ok": True, "code": "HANDOFF_SUBMITTED", "data": {"submitted": True}}


def frozen_bundle(projector, events, session_id, context, prepared=None):
    whole = StateProjector().project_visible(session_id, events)
    visible = context.visible(events, whole)
    if prepared is None:
        prepared = projector.prepare(
            events, visible, session_id, "", prefix=context.prefix
        )
    records = [
        {"source": session_id, "sequence": seq, "message": message}
        for seq, message in zip(prepared.source_sequences[1:], prepared.messages[1:])
    ]
    return {"records": records, **history_bundle(projector, events, session_id)}


def history_bundle(projector, events, session_id):
    """Original model messages and referenced artifacts at one Journal position."""
    whole = StateProjector().project_visible(session_id, events)
    raw = {}
    for item in _translate_visible_events(
        events, whole, "", projector.attachments_for(session_id)
    ):
        if item.sequence:
            raw.setdefault(str(item.sequence), []).append(item.message)
    artifacts = set()

    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "artifact_id" and is_valid_artifact_id(child):
                    artifacts.add(child)
                else:
                    collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    for values in raw.values():
        for message in values:
            if message["role"] == "tool":
                collect(json.loads(message["content"]))
    for event in events:
        payload = event.payload
        if isinstance(payload, DomainFactCommitted):
            if payload.fact_type == CREATED:
                artifacts.update((payload.data["artifact"], payload.data["request"]))
            elif payload.fact_type == WINDOW:
                artifacts.update((payload.data["context"], payload.data["bundle"]))
    return {"raw": raw, "artifacts": sorted(artifacts)}


def projected_tail(records, p, q):
    return [r["message"] for r in records if p < r["sequence"] <= q]


class CompactBoundary:
    def __init__(self, runtime, decision, context, config, control, transport):
        self.runtime, self.decision, self.context = runtime, decision, context
        self.config, self.control, self.transport = config, control, transport
        self.scheduler = None

    async def snapshot(self):
        sid = self.context.session_id
        events = await self.runtime.snapshot(sid)
        state = self.runtime.projector.project(sid, events).state
        if (
            state.waiting_command_ids
            or project_pending_approval(events) is not None
        ):
            return {"safe": False}
        visible = self.context.visible(
            events, StateProjector().project_visible(sid, events)
        )
        prompt = self.decision.prompt_for(state)
        tools = self.decision.schemas_for(state, events)[0]
        prepared = self.context.projector.prepare(
            events,
            visible,
            sid,
            prompt,
            prefix=self.context.prefix,
        )
        bundle = frozen_bundle(
            self.context.projector, events, sid, self.context, prepared
        )
        catalog = next(
            (
                thaw_value(e.payload.data)
                for e in reversed(events)
                if isinstance(e.payload, DomainFactCommitted)
                and e.payload.fact_type == "assistant.catalog"
            ),
            None,
        )
        return {
            "safe": True,
            "position": state.journal_position,
            "window": None
            if self.context.window is None
            else self.context.window["id"],
            "bundle": save_document(self.context.projector.gateway, sid, bundle),
            "inherited": save_document(
                self.context.projector.gateway,
                sid,
                {
                    "model": self.config.model_name,
                    "messages": prepared.messages,
                    "tools": tools,
                },
            ),
            "catalog": catalog,
        }

    async def publish(self, arguments):
        sid = self.context.session_id
        events = await self.runtime.snapshot(sid)
        current = window_fact(events)
        data = arguments["window"]
        if current is not None and current["id"] == data["id"]:
            if current != data:
                raise ValueError("conflicting context window publication")
            return True
        if project_parent(events) is not None and project_returned(events):
            return False
        if data["parent"] != (None if current is None else current["id"]):
            raise ValueError("stale handoff window")
        await self.runtime.receive_domain_fact(
            sid,
            CREATED,
            arguments["handoff"],
            source="compact",
            delivery_id=data["id"] + ":handoff",
        )
        await self.runtime.receive_domain_fact(
            sid, WINDOW, data, source="compact", delivery_id=data["id"] + ":window"
        )
        self.context.refresh(await self.runtime.snapshot(sid))
        return True

    async def before_advance(self):
        sid = self.context.session_id
        if self.context.is_reader:
            return True
        events = await self.runtime.snapshot(sid)
        parent = project_parent(events)
        if parent is not None and project_returned(events):
            return False
        state = self.runtime.projector.project(sid, events).state
        if state.waiting_command_ids or project_pending_approval(events) is not None:
            return True
        used = latest_input_tokens(events)
        response = await self.transport(
            "compact_boundary",
            sid,
            {"pressure": used is not None and used >= self.config.compact_threshold_tokens},
        )
        if response == "wait":
            return False
        if response != "continue":
            raise ValueError("invalid compact boundary response")
        return True
