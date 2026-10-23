"""初始化配置，并在终端中配置模型；凭据保存在用户数据目录。"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from thinllm import PROVIDERS, MissingProviderSetting, resolve_endpoint

from redpanda.config import AppConfig, config_path, load_app_config, write_json
from redpanda.llm.config import (
    ModelConfig, initial_connections, load_connections, parse_connections,
)
from redpanda.paths import RedPandaHome


def initialize():
    home = RedPandaHome.default()
    home.initialize()
    load_app_config()
    if not home.connections_path.exists():
        write_json(home.connections_path, initial_connections())
    load_connections(home.connections_path)
    return home


def needs_configuration(home: RedPandaHome) -> bool:
    model = load_app_config().default
    if model is None:
        return True
    provider = model.model.partition("/")[0]
    try:
        resolve_endpoint(model.model, load_connections(home.connections_path)[provider])
    except MissingProviderSetting:
        return True
    return False


def _ask(label: str, default: str = "", *, secret: bool = False, required: bool = False) -> str:
    suffix = ("（回车保留已配置密钥）" if secret and default else
              f" [{default}]" if default else "")
    while True:
        value = (getpass.getpass if secret else input)(label + suffix + ": ").strip() or default
        if value or not required:
            return value
        print("此项不能为空。")


def configure(home: RedPandaHome) -> ModelConfig:
    app = load_app_config()
    connections = load_connections(home.connections_path)
    names = tuple(PROVIDERS)
    print("\n配置编码模型（Ctrl+C 取消）")
    for index, name in enumerate(names, 1):
        print(f"  {index}. {name}")
    default_provider = "" if app.default is None else app.default.model.partition("/")[0]
    while True:
        provider = _ask("供应商名称或序号", default_provider)
        if provider.isascii() and provider.isdecimal() and 1 <= int(provider) <= len(names):
            provider = names[int(provider) - 1]
        if provider in PROVIDERS:
            break
        print("请选择上面列出的供应商。")

    specification = PROVIDERS[provider]
    connection = dict(connections[provider])
    if specification.uses_codex_auth:
        print("请先运行 python -m thinllm auth codex 获取连接配置；认证文件由 Codex 管理。")
        connection["auth_file"] = _ask("Codex auth.json 路径（留空使用 CODEX_HOME 或 ~/.codex）",
                                       connection["auth_file"])
    else:
        connection["api_key"] = _ask(
            "API Key" + ("（本地服务未启用认证时可留空）" if not specification.api_key_required else ""),
            connection["api_key"], secret=True, required=specification.api_key_required,
        )
    if specification.base_url is None:
        while True:
            connection["base_url"] = _ask("服务地址（包含 /v1）", connection["base_url"], required=True)
            try:
                parse_connections({**connections, provider: connection})
            except ValueError as error:
                print(error)
            else:
                break

    previous_default = app.default
    default_name = (previous_default.model.partition("/")[2] if previous_default is not None
                    and previous_default.model.startswith(provider + "/") else "")
    name = _ask("模型 ID（填写服务实际接受的名称）", default_name, required=True)
    model = provider + "/" + name
    previous = next((item for item in app.models if item.model == model), None)
    print("compact 阈值是触发上下文整理的输入 token 数，请按模型容量留出余量。")
    while True:
        threshold = _ask("compact 阈值", "" if previous is None else str(previous.compact_threshold_tokens))
        if threshold.isascii() and threshold.isdecimal() and int(threshold) > 0:
            break
        print("请输入大于 0 的整数。")
    effort = None
    if model == "stepfun/step-5-preview":
        while True:
            effort = _ask("推理程度（default/low/medium/high）",
                          previous.reasoning_effort if previous and previous.reasoning_effort else "default")
            if effort in ("default", "low", "medium", "high"):
                effort = None if effort == "default" else effort
                break
            print("请选择 default、low、medium 或 high。")
    profile = ModelConfig(model, int(threshold), effort)
    candidates = tuple(profile if item.model == model else item for item in app.models)
    if previous is None:
        candidates += (profile,)
    updated = AppConfig(model, candidates)
    connections[provider] = connection
    parse_connections(connections)
    resolve_endpoint(model, connection)
    write_json(home.connections_path, connections)
    write_json(config_path(), updated.to_dict())
    print(f"已保存，默认模型：{model}")
    return profile


async def _test_model(home: RedPandaHome, profile: ModelConfig) -> dict:
    from redpanda.assistant.model_connection import check_model_connection
    from redpanda.llm.connections import ModelConnections

    async with ModelConnections(home.connections_path) as llm:
        return await check_model_connection(llm, profile.model, reasoning_effort=profile.reasoning_effort)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--configure", action="store_true", help="在交互终端添加或修改默认模型")
    mode.add_argument("--if-needed", action="store_true", help="未填写默认模型或连接时进入向导")
    args = parser.parse_args(argv)
    home = initialize()
    print(f"模型连接配置：{home.connections_path}")
    print(f"候选模型配置：{config_path()}")
    needed = needs_configuration(home)
    if args.if_needed and not needed:
        print("默认模型与连接设置已填写，保留现有配置。")
        return 0
    if not args.configure and not (args.if_needed and needed):
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        message = "请在交互终端运行项目 Python -m redpanda.initialize --configure，或编辑上面的配置文件。"
        if args.configure:
            parser.error(message)
        print(message)
        return 0
    try:
        profile = configure(home)
        if _ask("是否发送一条短请求测试连接？（y/N）").lower() in ("y", "yes"):
            result = asyncio.run(_test_model(home, profile))
            print(f"{result['message']}（{result['elapsed_ms']} ms）")
            if not result["ok"]:
                print("配置已保存，可重新运行向导修改，或稍后在 Web 中测试。")
                return 1
    except (KeyboardInterrupt, EOFError):
        print("\n配置向导已取消。")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
