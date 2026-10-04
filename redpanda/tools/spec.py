"""与工具来源无关的参数契约与 ToolSpec。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Mapping, Protocol

from redpanda.tools.control import ControlApprovalProposal, ControlPreparationFailure

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from pydantic.json_schema import GenerateJsonSchema

# ---------------------------------------------------------------------------
# 参数描述与运行时校验必须来自同一个契约。
# ---------------------------------------------------------------------------


class ToolArgumentsError(Exception):
    """模型提供的工具参数不符合工具契约。"""

    def __init__(self, details: Any) -> None:
        super().__init__("tool arguments validation failed")
        self.details = details


class ToolParameters(Protocol):
    def schema(self) -> Mapping[str, Any]:
        ...

    def validate(self, payload: Any) -> Any:
        ...


class _NoAutoTitles(GenerateJsonSchema):
    """不给字段自动补 title。

    自动 title 只是把字段名换个大小写，而 property key 就在模型眼前。显式写的
    `Field(title=...)` 不走这条路，仍会留在 schema 里。
    """

    def field_title_should_be_set(self, schema: Any) -> bool:
        return False


def _drop_class_metadata(schema: dict[str, Any]) -> None:
    """类名和 class docstring 是写给代码读者的，不进模型上下文。

    参数对象要对模型说的话属于 `ToolSpec.description`，那一份是必填的；两处都
    说等于每次请求付两遍。只清对象自身和 `$defs` 里嵌套模型的这一层，字段级的
    title 由 `_NoAutoTitles` 负责，剩下的都是作者显式写的。
    """

    for node in (schema, *_definitions(schema)):
        node.pop("title", None)
        node.pop("description", None)


def _definitions(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    definitions = schema.get("$defs")
    if not isinstance(definitions, Mapping):
        return []
    return [
        definition
        for definition in definitions.values()
        if isinstance(definition, dict)
    ]


@dataclass(frozen=True)
class PydanticParameters:
    input_model: type[BaseModel]

    def schema(self) -> Mapping[str, Any]:
        schema = self.input_model.model_json_schema(
            schema_generator=_NoAutoTitles,
        )
        _drop_class_metadata(schema)
        schema["additionalProperties"] = False
        return schema

    def validate(self, payload: Any) -> BaseModel:
        try:
            return self.input_model.model_validate(
                payload,
                strict=True,
                extra="forbid",
            )
        except PydanticValidationError as exc:
            raise ToolArgumentsError(
                exc.errors(include_context=False)
            ) from exc


@dataclass(frozen=True, init=False)
class JsonSchemaParameters:
    """直接使用外部 JSON Schema，不生成 Pydantic Model 或改写语义。"""

    _schema: dict[str, Any] = field(repr=False)
    _validator: Any = field(init=False, repr=False, compare=False)

    def __init__(self, input_schema: Mapping[str, Any]) -> None:
        schema = deepcopy(dict(input_schema))
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError(
                "tool parameters JSON Schema 顶层 type 必须显式为 object"
            )
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_validator", validator_class(schema))

    def schema(self) -> Mapping[str, Any]:
        return deepcopy(self._schema)

    def validate(self, payload: Any) -> dict[str, Any]:
        try:
            self._validator.validate(payload)
        except JsonSchemaValidationError as exc:
            raise ToolArgumentsError(
                {
                    "message": exc.message,
                    "path": list(exc.absolute_path),
                    "schema_path": list(exc.absolute_schema_path),
                    "validator": exc.validator,
                }
            ) from exc
        return payload


# 授权判定钩子：签发时对工具参数求值，True = 需要用户确认（ask）。
# 必须是纯函数，只看参数。静态布尔是它的常量特例。
AuthorizationPolicy = Callable[[Mapping[str, object]], bool]


@dataclass(frozen=True)
class ToolSpec:
    """与工具来源和模型 Provider 无关的内部工具定义。"""

    name: str
    description: str
    parameters: ToolParameters
    handler: Callable[
        [Any],
        Awaitable[
            dict[str, Any] | ControlApprovalProposal | ControlPreparationFailure
        ],
    ]
    control_boundary: bool = False
    exclusive_batch: bool = False
    requires_authorization: bool | AuthorizationPolicy = False

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("tool name must be a non-empty str")
        if type(self.description) is not str or not self.description:
            raise ValueError("tool description must be a non-empty str")
        if not callable(self.handler):
            raise TypeError("tool handler must be callable")
        if type(self.control_boundary) is not bool:
            raise TypeError("control_boundary must be bool")
        if type(self.exclusive_batch) is not bool:
            raise TypeError("exclusive_batch must be bool")
        if type(self.requires_authorization) is not bool and not callable(
            self.requires_authorization
        ):
            raise TypeError("requires_authorization must be bool or AuthorizationPolicy")

    def to_openai_tool(self) -> dict[str, Any]:
        """导出当前 OpenAI-compatible 模型接口所需的工具格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters.schema()),
            },
        }


class EmptyInput(BaseModel):
    """无参工具的占位输入模型。"""


def pydantic_tool_spec(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    handler: Callable[
        [BaseModel],
        Awaitable[
            dict[str, Any] | ControlApprovalProposal | ControlPreparationFailure
        ],
    ],
    control_boundary: bool = False,
    exclusive_batch: bool = False,
    requires_authorization: bool | AuthorizationPolicy = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=PydanticParameters(input_model),
        handler=handler,
        control_boundary=control_boundary,
        exclusive_batch=exclusive_batch,
        requires_authorization=requires_authorization,
    )
