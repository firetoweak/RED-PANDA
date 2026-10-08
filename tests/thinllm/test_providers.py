import unittest

from thinllm import MissingProviderSetting, resolve_endpoint


class ResolveEndpointTest(unittest.TestCase):
    def test_required_settings_come_from_the_given_connection(self):
        for model, variable in (
            ("deepseek/deepseek-v4-pro", "api_key"),
            ("openai/gpt-4.1", "api_key"),
            ("stepfun/step-3.5-flash", "api_key"),
            ("bigmodel/glm-5.2", "api_key"),
            ("vllm/Qwen/Qwen3-8B", "base_url"),
            ("ollama/qwen3:8b", "base_url"),
        ):
            with self.subTest(model=model):
                with self.assertRaises(MissingProviderSetting) as missing:
                    resolve_endpoint(model, {"api_key": "", "base_url": ""})
                self.assertEqual(missing.exception.setting, variable)

    def test_hosted_provider_uses_its_official_endpoint_and_key(self):
        for model, variable, base_url in (
            ("openai/gpt-4.1", "api_key", "https://api.openai.com/v1"),
            ("stepfun/step-3.5-flash", "api_key", "https://api.stepfun.com/step_plan/v1"),
            ("bigmodel/glm-5.2", "api_key", "https://open.bigmodel.cn/api/paas/v4"),
        ):
            with self.subTest(model=model):
                endpoint = resolve_endpoint(model, {variable: "provider-key"})

                self.assertEqual(endpoint.provider, model.partition("/")[0])
                self.assertEqual(endpoint.base_url, base_url)
                self.assertEqual(endpoint.api_key, "provider-key")
                self.assertFalse(endpoint.pads_reasoning_content)

    def test_local_provider_takes_its_address_and_an_optional_key(self):
        for model, address_variable, key_variable, base_url in (
            (
                "vllm/Qwen/Qwen3-8B", "base_url", "api_key",
                "http://127.0.0.1:8000/v1",
            ),
            (
                "ollama/qwen3:8b", "base_url", "api_key",
                "http://127.0.0.1:11434/v1",
            ),
        ):
            for key in (None, "provider-key"):
                with self.subTest(model=model, key=key):
                    environ = {address_variable: base_url + "/", key_variable: ""}
                    if key is not None:
                        environ[key_variable] = key
                    endpoint = resolve_endpoint(model, environ)

                    self.assertEqual(endpoint.provider, model.partition("/")[0])
                    self.assertEqual(endpoint.base_url, base_url)
                    self.assertEqual(endpoint.api_key, key)
                    self.assertFalse(endpoint.pads_reasoning_content)
