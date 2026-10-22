"""main.py 的使用大模型插件筛选逻辑验证。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parent.parent


def load_main():
    loader = importlib.util.spec_from_file_location("wxbot_main", ROOT / "main.py")
    assert loader is not None
    module = importlib.util.module_from_spec(loader)
    assert loader.loader is not None
    loader.loader.exec_module(module)
    return module


class UsesLLMTests(unittest.TestCase):
    def spec_at(self, directory):
        return types.SimpleNamespace(path=directory / "__init__.py")

    def test_manifest_llm_true_flag_selects_plugin(self):
        main = load_main()
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base)
            (directory / "plugin.json").write_text(
                json.dumps({"api": "v1", "llm": True}), encoding="utf-8-sig"
            )
            self.assertTrue(main.uses_llm(self.spec_at(directory)))

    def test_absent_or_false_llm_flag_is_not_selected(self):
        main = load_main()
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base)
            (directory / "plugin.json").write_text(
                json.dumps({"api": "v1"}), encoding="utf-8-sig"
            )
            self.assertFalse(main.uses_llm(self.spec_at(directory)))

    def test_missing_manifest_is_not_selected(self):
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base)
            self.assertFalse(load_main().uses_llm(self.spec_at(directory)))


if __name__ == "__main__":
    unittest.main()
