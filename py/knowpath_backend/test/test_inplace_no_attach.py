"""就地模式回归:attach/export 工具与 Workspace 方法已彻底移除。"""
import importlib
import pytest

from knowpath_backend.workspace import Workspace


def test_attach_tools_removed():
    for mod in ("attach_file", "attach_dir", "export_file"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"knowpath_backend.tools.builtin.{mod}")


def test_workspace_attach_methods_removed(tmp_path):
    ws = Workspace(tmp_path)
    for attr in ("attach", "attach_dir", "origin_of", "manifest"):
        assert not hasattr(ws, attr), f"{attr} 应已删除"
