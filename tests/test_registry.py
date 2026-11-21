import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mcp_offline_core.registry import ConfigurationError, load_configuration, load_registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.config = self.directory / "server.toml"
        self.manifest = {
            "contract_version": 1,
            "module_id": "sample",
            "tools": [{
                "name": "sample_echo",
                "description": "Return the input.",
                "handler": "uninstalled_package.module:echo",
                "input_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"$ref": "#/$defs/short"}},
                    "$defs": {"short": {"type": "string", "maxLength": 3}},
                },
            }],
        }

    def load(self, second=None):
        (self.directory / "first.json").write_text(json.dumps(self.manifest))
        config = 'config_version = 1\ndisabled_tools = ["sample_echo"]\n[[modules]]\nmanifest = "first.json"\n'
        if second is not None:
            (self.directory / "second.json").write_text(json.dumps(second))
            config += '[[modules]]\nmanifest = "second.json"\n'
        self.config.write_text(config)
        return load_registry(load_configuration(self.config))

    def test_schema_and_registration_do_not_import_handler(self):
        tool = self.load()["sample_echo"]
        self.assertTrue(tool.validator.is_valid({"text": "yes"}))
        self.assertFalse(tool.validator.is_valid({"text": "long"}))
        self.assertFalse(tool.validator.is_valid({"text": 5}))

    def test_duplicate_module_and_disabled_tool(self):
        with self.assertRaisesRegex(ConfigurationError, "sample"):
            self.load(self.manifest)
        with self.assertRaisesRegex(ConfigurationError, "sample_echo"):
            self.load(dict(self.manifest, module_id="second"))

    def test_bad_manifest_version_and_schema(self):
        self.manifest["contract_version"] = 2
        with self.assertRaisesRegex(ConfigurationError, "contract_version"):
            self.load()
        self.manifest["contract_version"] = 1
        tool = self.manifest["tools"][0]
        for invalid in (
            {"type": "array"},
            {"type": "object", "properties": {"text": {"$ref": "https://example.invalid/schema"}}},
            {"type": "object", "$ref": "#/unknown"},
            {"type": "object", "required": False},
        ):
            with self.subTest(schema=invalid):
                tool["input_schema"] = invalid
                with self.assertRaisesRegex(ConfigurationError, "input_schema"):
                    self.load()
