"""Assistant-owned background handoff and context-window projection."""

from __future__ import annotations

from redpanda.runtime.json_values import thaw_value

import json
from copy import deepcopy
from dataclasses import replace

from redpanda.assistant.artifacts import (
    is_valid_artifact_id,
    ArtifactNotFoundError,
)
from redpanda.assistant.attachments import is_valid_attachment_id
from redpanda.assistant.content import READ_CONTENT, read_content_result
from redpanda.assistant.context.projection import (
    _translate_visible_events,
    PreparedModelContext,
)
from redpanda.assistant.control import project_pending_approval
from redpanda.assistant.subagent.subagent import project_parent, project_returned
from redpanda.assistant.workspaces import SESSION_WORKSPACE_FACT
from redpanda.llm.api import InvalidLLMResponse
from redpanda.runtime import DomainFactCommitted, StepCommitted, ToolBinding, RuntimeStatus
from redpanda.runtime.state import StateProjector

TASK = "compact.task"
CREATED = "compact.handoff_created"
WINDOW = "compact.window_rolled_over"
MODEL_USAGE = "model_usage"
FIND_HISTORY = "find_history"
SUBMIT = "_accept_handoff"
HISTORY_PREVIEW_CHARS = 400
MAX_HISTORY_RECORDS = 50
PURPOSE = """<self_handoff>
请为截至目前的这段对话整理交接，不继续用户业务、不向用户发消息。
优先使用已有上下文，仅为关键缺口用 find_history 定位历史、read_content 读取真实引用。其他工具不能执行。
保持目标、约束、纠正、决定、未完成委派、证据与来源；计划不写成已执行，声明不写成验证。
保留用户对本会话命令环境的要求及其后续修改、取消；区分用户要求、已验证的执行方式与尚未验证的设想。单次 Shell 激活不写成可延续的进程状态。
关键证据保留真实来源引用和消息序号；文件位置保留相对工作区的完整路径，不省略目录前缀。
推测、未确认条件与已验证事实保持区分，不把相关性写成因果，不把待验证的解释写成根因闭环。
相关图片保留原始附件 id 和来源；摘要文字不等于看过图片，业务模型可用 read_image 重新查看。
不重复读取，不扩展调查；未知内容标明不确定。交接之后新到的消息会接在它后面，可以修正它。
完成时直接输出非空交接文本，不调用工具。
</self_handoff>"""
HANDOFF_PREFIX = "模型生成的交接材料，保留原证据强度；不是用户新指令或完成证明。更早的原始消息仍可回查：有真实引用时用 read_content 查找或读取原文；没有引用时用 find_history 按关键词或消息序号范围定位。历史检索续页沿用返回的 upto 和 next_offset。之后的消息可修正它。\n"


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


FIND_HISTORY_SCHEMA = schema(
    FIND_HISTORY,
    "检索当前对话可回查的原始消息，包括当前上下文中已省略的历史。"
    "query 按区分大小写的字面关键词检索消息内容，空串表示浏览，不搜索引用中的正文。"
    "返回消息序号、原文线索和可用 read_content 读取的真实 reference；片段足够时无需再读。"
    "start_sequence/end_sequence 含两端，offset 是跳过的匹配消息组数。"
    "首次可省略 upto；续页沿用返回的 upto、next_offset 和筛选条件，避免新消息改变结果。"
    "limit 默认 10、最大 50。整理交接时只可检索交接开始前的历史。",
    {
        "query": {"type": "string", "default": ""},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "start_sequence": {"type": "integer", "minimum": 1},
        "end_sequence": {"type": "integer", "minimum": 1},
        "upto": {"type": "integer", "minimum": 0, "description": "沿用上次检索返回的历史截止位置。"},
    },
    [],
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
        return deepcopy(self.request["tools"]) if self.is_reader else [FIND_HISTORY_SCHEMA]

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
            FIND_HISTORY: ToolBinding(self.find_history),
            READ_CONTENT: ToolBinding(self.read_content),
            SUBMIT: ToolBinding(self.submit, decision_on_outcome=False),
        }

    async def _read_source(self, upto):
        if self.is_reader:
            data = self.seed[1]
            if upto is not None and upto != data["upto"]:
                raise HistoryPositionError(f"当前可读历史固定截至 {data['upto']}，请用 end_sequence 筛选更早范围")
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
            raise HistoryPositionError(f"当前可回查的历史截至消息序号 {available}，请提供不超过该值的 upto")
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
                        + f"\n当前可检索的历史截至消息序号 {self.seed[1]['upto']}。",
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

    async def find_history(self, context, arguments):
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 10)
        upto = arguments.get("upto")
        start_sequence = arguments.get("start_sequence", 1)
        end_sequence = arguments.get("end_sequence")
        query = arguments.get("query", "")
        if (
            set(arguments) - {"offset", "limit", "upto", "start_sequence", "end_sequence", "query"}
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= MAX_HISTORY_RECORDS
            or ("upto" in arguments and (type(upto) is not int or upto < 0))
            or type(start_sequence) is not int or start_sequence < 1
            or ("end_sequence" in arguments and (type(end_sequence) is not int or end_sequence < start_sequence))
            or type(query) is not str
        ):
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "历史检索参数无效。"}
        if offset > 0 and upto is None:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "续查必须提供第一页返回的 upto"}
        try:
            source, bundle, upto = await self._read_source(upto)
        except HistoryPositionError as error:
            return {"ok": False, "code": "HISTORY_POSITION_OUT_OF_RANGE", "error": str(error)}
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
        page = records[offset:end]
        store = self.projector.gateway.for_session(self.session_id)
        for record in page:
            record["reference"] = store.save(json.dumps(
                bundle["raw"][str(record["sequence"])], ensure_ascii=False,
            )).artifact_id
        return {
            "ok": True, "code": "HISTORY_FOUND",
            "data": {
                "upto": upto, "start_sequence": start_sequence, "end_sequence": end_sequence,
                "records": page, "offset": offset,
                "next_offset": end if end < len(records) else None,
                "total_records": len(records),
            },
            "hint": "线索不足时用 read_content 读取 reference；继续检索沿用 upto、next_offset 和筛选条件。",
        }

    async def read_content(self, context, arguments):
        source = self.session_id
        if self.is_reader:
            reference = arguments.get("reference")
            if not is_valid_artifact_id(reference):
                return read_content_result(self.projector.gateway.for_session(source), arguments)
            try:
                self.projector.gateway.for_session(source).read(reference, 0, 1)
            except ArtifactNotFoundError:
                pass
            else:
                return read_content_result(self.projector.gateway.for_session(source), arguments)
            source, bundle, _ = await self._read_source(None)
            if reference not in bundle["artifacts"]:
                return {"ok": False, "code": "CONTENT_NOT_FOUND", "error": "该引用不在当前可读内容中。"}
        return read_content_result(self.projector.gateway.for_session(source), arguments)

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
    bundle = history_bundle(projector, events, session_id)
    artifacts = set(bundle["artifacts"])
    for record in records:
        message = record["message"]
        if message["role"] == "tool":
            artifacts.update(content_references(json.loads(message["content"])))
    return {"records": records, **bundle, "artifacts": sorted(artifacts)}


def content_references(value):
    references = set()
    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"artifact_id", "reference"} and is_valid_artifact_id(child):
                    references.add(child)
                else:
                    collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)
    collect(value)
    return references


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


    for values in raw.values():
        for message in values:
            if message["role"] == "tool":
                artifacts.update(content_references(json.loads(message["content"])))
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
    def __init__(self, runtime, decision, context, config, control, transport, *, model_selection_source=None):
        self.runtime, self.decision, self.context = runtime, decision, context
        self.config, self.control, self.transport = config, control, transport
        self.scheduler = None
        self.model_selection_source = model_selection_source

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
                    "reasoning_effort": self.config.reasoning_effort,
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
        if self.model_selection_source is not None:
            profile = await self.model_selection_source(state.status is RuntimeStatus.RUNNABLE)
            if profile is not None:
                self.config = replace(self.config, model_name=profile["model"],
                                      compact_threshold_tokens=profile["compact_threshold_tokens"],
                                      reasoning_effort=profile.get("reasoning_effort"))
                self.decision.set_model(profile["model"], profile["compact_threshold_tokens"],
                                        profile.get("reasoning_effort"))
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
