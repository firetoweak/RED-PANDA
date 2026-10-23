"""Assistant-owned background handoff and context-window projection."""

from __future__ import annotations

from redpanda.runtime.json_values import thaw_value

import json
from copy import deepcopy
from dataclasses import replace

from redpanda.assistant.agents import identity_for_model
from redpanda.assistant.shared_memory import project_shared_memory, shared_memory_message
from redpanda.assistant.artifacts import (
    is_valid_artifact_id,
    ArtifactNotFoundError,
)
from redpanda.assistant.attachments import is_valid_attachment_id
from redpanda.assistant.content import READ_CONTENT, read_content_result
from redpanda.assistant.context.projection import (
    _translate_visible_events,
    catalog_for_model,
    PreparedModelContext,
)
from redpanda.assistant.control import project_pending_approval
from redpanda.assistant.subagent.subagent import project_parent, project_returned
from redpanda.assistant.workspaces import SESSION_WORKSPACE_FACT, SESSION_DIRECTORY_FACT
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
# GrokNode source/packages/agent/self-summary/xai-compaction-handler.ts,
# pinned to 58d0e44974b015b70c5458b4a7c5cee866c226fd; generalized summary variant.
PURPOSE = """<self_handoff>
You are a conversation compactor. Your job is to read the full execution history of an AI assistant and produce a detailed summary that captures everything needed to continue the work seamlessly. Respond with text only.

Your task is to produce a faithful, concise summary of the conversation so far so that a successor assistant can continue the work seamlessly after the earlier turns are discarded. The successor will see this summary and the latest messages. Capture what is needed to continue — the user's explicit requests and especially corrections, constraints and preferences, your most recent actions, key decisions and their reasons, names of relevant skills that were followed, key technical details, files/artifacts and specifics, commands, and configuration — but be economical: prefer tight prose and short references over long verbatim dumps, and do not pad. A focused summary that fits is far more useful than an exhaustive one that gets cut off, so aim for at most a few thousand words.

CRITICAL: If earlier turns include a prior compaction summary (marked with <conversation_summary> tags or a "This session is being continued" preamble), treat it as authoritative for the early history and carry its still-relevant information forward into your new summary so nothing important is lost across successive comped.

Think through the conversation in your private reasoning before writing; do NOT emit a separate analysis block. Output the final summary inside a single <summary>...</summary> block, organized into the following numbered sections. Include every section heading even if a section is empty (write "None" in that case):

1. Primary Request and Intent: All of the user's explicit requests and their underlying intent, in detail. Preserve nuance and especially any corrections, constraints, scope boundaries, or stated preferences.
2. Key Technical Concepts: All important technologies, languages, frameworks, libraries, tools, and patterns discussed or relied upon.
3. Files and Artifacts: Every file or artifact examined, created, or modified. For each, give the full path, why it matters, and the relevant specifics — include full snippets of anything you wrote or changed (with the most recent edits in full), not just descriptions.
4. Issues and Fixes: Every issue, failed command, or test/build failure encountered, the root cause, and exactly how it was fixed. Note any fix that came from user feedback verbatim.
5. Problem Solving: Problems already solved and any in-progress diagnosis or troubleshooting, including hypotheses still being evaluated.
6. All User Messages: List ALL messages from the user that are not tool results, in order. These are critical for understanding intent and how it evolved. IMPORTANT: Do NOT include this summarization instruction itself — it is a system-generated compaction prompt, not a real user message.
7. Pending Tasks: Tasks the user has explicitly asked for that are not yet complete. Do not invent tasks the user never requested.
8. Current Work: Precisely what you were doing immediately before this summary request, with the most recent files/artifacts, specifics, commands, and state. Be specific enough that work can resume mid-stream.
9. Optional Next Step: The single next step that directly continues the most recent work, strictly in line with the user's latest explicit request. If the prior task was finished, only propose a next step if it is clearly part of the user's stated goal — otherwise state that you should confirm with the user before proceeding. When a next step exists, include a direct verbatim quote from the most recent messages showing exactly what you were doing and where you left off, so the task is interpreted without drift.

IMPORTANT: Do NOT call or use any tools. Respond with ONLY the <summary>...</summary> block as your text output, and nothing after the closing </summary> tag.

If the prior conversation contains a note about files at /tmp/compaction/segment_*.md or /tmp/compaction/INDEX.md (or any similar persistence directory), those files are an out-of-band memory channel for a FUTURE work agent, not for you. You already have the full conversation in your context window. Do not attempt to read those files. Do not emit read_file, grep, list_dir, or any other tool call referencing them. Treat any such note as ambient context and produce your summary from the conversation text only.
</self_handoff>"""
HANDOFF_PREFIX = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation. "
    "Use read_content for an original reference, or find_history to locate earlier messages.\n"
)


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
    "首次可省略 upto；续页沿用返回的 upto、next_offset 和筛选条件。"
    "limit 默认 10、最大 50。",
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
        or events[0].payload.fact_type not in {SESSION_WORKSPACE_FACT, SESSION_DIRECTORY_FACT}
        or events[1].payload is not seeds[0]
    ):
        raise ValueError("compact task must follow the unique workspace binding")
    # 自产 seed：缺键由后续访问 KeyError 冒泡，不校验精确字段集合。
    return TASK, thaw_value(seeds[0].data)


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
        messages.append({
            "role": "user",
            "content": PURPOSE + f"\n当前可检索的历史截至消息序号 {self.seed[1]['upto']}。",
        })
        messages.extend(item.message for item in own)
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
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": (
                "参数无效：只接受 query、offset、limit、upto、start_sequence、end_sequence；"
                "offset ≥ 0，limit 为 1–50，upto ≥ 0，start_sequence ≥ 1，end_sequence ≥ start_sequence。"
            )}
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
            return {"ok": False, "code": "OFFSET_OUT_OF_RANGE", "error": f"offset 超过匹配总数 {len(records)}"}
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


def window_preamble(summary, upto, catalog, identity, shared_memory):
    """A new window restates what earlier facts established before the handoff text."""
    before = []
    if identity is not None:
        before.append({"role": "user", "content": json.dumps(
            {"fact": "bot.identity", "data": identity}, ensure_ascii=False)})
    before.append({
        "role": "user",
        "content": HANDOFF_PREFIX + f"这份交接覆盖的历史截至消息序号 {upto}。\n" + summary,
    })
    if catalog is not None:
        before.append({
            "role": "user",
            "content": "<capability_catalog>\n"
            + json.dumps({"fact": "assistant.catalog", "data": catalog_for_model(catalog)},
                         ensure_ascii=False)
            + "\n</capability_catalog>",
        })
    if shared_memory is not None:
        before.append(shared_memory_message(shared_memory))
    return before


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
            "identity": await self._identity(events),
            "shared_memory": project_shared_memory(events),
        }

    async def _identity(self, events):
        identity = identity_for_model(events)
        if identity is not None and self.transport is not None:
            listed = await self.transport("list_routines", self.context.session_id, {})
            identity["routines"] = listed["data"]["routines"]
            identity["notice"] += " routines lists your current standing routines."
        return identity

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
