import unittest

from thinllm import MissingProviderSetting, resolve_endpoint


class ResolveEndpointTest(unittest.TestCase):
    def test_required_settings_come_from_the_given_environment(self):
        with self.assertRaises(MissingProviderSetting) as missing:
            resolve_endpoint("deepseek/deepseek-v4-pro", {})
        self.assertEqual(missing.exception.variable, "DEEPSEEK_API_KEY")

        with self.assertRaises(MissingProviderSetting) as missing:
            resolve_endpoint("vllm/Qwen/Qwen3-8B", {})
        self.assertEqual(missing.exception.variable, "VLLM_BASE_URL")

    def test_local_provider_takes_its_address_and_an_optional_key(self):
        endpoint = resolve_endpoint(
            "vllm/Qwen/Qwen3-8B", {"VLLM_BASE_URL": "http://127.0.0.1:8000/v1/"}
        )

        self.assertEqual(endpoint.provider, "vllm")
        self.assertEqual(endpoint.base_url, "http://127.0.0.1:8000/v1")
        self.assertIsNone(endpoint.api_key)
        self.assertFalse(endpoint.pads_reasoning_content)
