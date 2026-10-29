import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from konden.config import write_json
from konden.llm.config import initial_connections
from konden.model_settings import ModelConfigurationError
from konden.search import SearchConfig, SearchSettings


def test_search_preferences_are_separate_from_models_and_credentials(tmp_path):
    connections = initial_connections()
    connections["openai"]["api_key"] = "private-key"
    path = tmp_path / "connections.json"
    write_json(path, connections)
    config_path = tmp_path / "config.json"
    config_path.write_text('{"model":{"default":null,"candidates":[]}}', encoding="utf-8")
    before = path.read_bytes(), config_path.read_bytes()
    settings = SearchSettings(path)
    selected = SearchConfig(enabled=True, provider="openai", openai_model="openai/gpt-6.1-terra")
    settings.save(selected)
    settings.save(selected.model_copy(update={"enabled": False}))
    assert SearchSettings(path).config().model_dump() == {**selected.model_dump(), "enabled": False}
    assert before == (path.read_bytes(), config_path.read_bytes())
    assert "private-key" not in settings.path.read_text(encoding="utf-8")


def test_missing_connection_cannot_replace_saved_search_preferences(tmp_path):
    path = tmp_path / "connections.json"
    write_json(path, initial_connections())
    settings = SearchSettings(path)
    with pytest.raises(ModelConfigurationError, match="openai.api_key"):
        settings.save(SearchConfig(provider="openai", openai_model="openai/gpt-6.1-terra"))
    assert not settings.path.exists()
    with pytest.raises(ValidationError, match="搜索模型"):
        SearchConfig(provider="openai")


def test_deepseek_test_returns_real_sources_without_inventing_an_answer(tmp_path):
    connections = initial_connections()
    connections["deepseek"]["api_key"] = "private-key"
    path = tmp_path / "connections.json"
    write_json(path, connections)
    def handle(request):
        assert request.url == "https://api.deepseek.com/v1/tools/search"
        assert json.loads(request.content)["query"] == "question"
        return httpx.Response(200, json={"results": [{"title": "Source", "url": "https://example.com/",
            "snippet": "Original snippet", "summary": "summary", "content": "", "date": None,
            "site_name": None, "site_icon": None, "rerank_score": 0.5, "authority": 1}]})
    result = asyncio.run(SearchSettings(path).search("question", SearchConfig(), transport=httpx.MockTransport(handle)))
    assert result["searched"] and result["summary"] is None
    assert result["sources"] == [{"title": "Source", "url": "https://example.com/", "snippet": "Original snippet"}]
    assert not SearchSettings(path).path.exists()
