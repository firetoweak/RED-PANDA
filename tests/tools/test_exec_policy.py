import unittest

from redpanda.tools.builtin.exec_policy import command_requires_authorization
from redpanda.tools.spec import ToolSpec, PydanticParameters, EmptyInput


class ExecPolicyTest(unittest.TestCase):
    def test_ask_for_dangerous_commands(self):
        for command in (
            "rm -rf /tmp/x",
            "Remove-Item C:\\x -Recurse",
            "del /f secret.txt",
            "git push --force origin main",
            "git push -f",
            "git push --force-with-lease",
            "git   reset   --hard",
            "gh repo delete owner/repo",
            "format C:",
            "shutdown /s /t 0",
            "Restart-Computer",
        ):
            with self.subTest(command=command):
                self.assertTrue(
                    command_requires_authorization({"command": command})
                )

    def test_allow_for_benign_commands(self):
        for command in (
            "rg pattern",
            "git status",
            "git push origin main",
            "git push -follow-tags",
            "gh repo view owner/repo",
            "python -m pytest",
            "where.exe rg",
        ):
            with self.subTest(command=command):
                self.assertFalse(
                    command_requires_authorization({"command": command})
                )

    def test_non_string_command_allows(self):
        self.assertFalse(command_requires_authorization({}))
        self.assertFalse(command_requires_authorization({"command": 42}))


class ToolSpecAuthorizationValidationTest(unittest.TestCase):
    def test_requires_authorization_accepts_bool_or_callable(self):
        async def handler(_input):
            return {"ok": True, "code": "OK"}

        for value in (True, False, lambda _args: True):
            spec = ToolSpec(
                "t",
                "desc",
                PydanticParameters(EmptyInput),
                handler,
                requires_authorization=value,
            )
            self.assertIs(spec.requires_authorization, value)

    def test_requires_authorization_rejects_other_types(self):
        async def handler(_input):
            return {"ok": True, "code": "OK"}

        with self.assertRaises(TypeError):
            ToolSpec(
                "t",
                "desc",
                PydanticParameters(EmptyInput),
                handler,
                requires_authorization="yes",
            )
