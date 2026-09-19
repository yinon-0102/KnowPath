"""Reusable Agent configuration and assembly, independent of terminal UI."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv

from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
from knowpath_backend.core.llm import MyLLM
from knowpath_backend.memory import LLMSummarizer, MemoryConfig, MemoryManager, OpenAIEmbedding
from knowpath_backend.tools.registry import ToolRegistry
from knowpath_backend.tools.builtin.calculator import CalculatorTool

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_DIR = Path.home() / ".my_companion"


CONFIG_PATH = DEFAULT_STORAGE_DIR / "config.json"


DEFAULT_CONFIG: Dict = {
    # LLM
    "provider_key": "openai",
    "provider":     "openai",
    "model":        "",
    "api_key":      "",
    "base_url":     None,
    # Memory
    "memory": {
        "cold_backend":      "sqlite",
        "vector_backend":    "sqlite",
        "conflict_strength": "fast",     # 默认 fast,不依赖 embedding API
        "tick_mode":         "async",
        "use_embedding":     False,      # 默认关,避免没 embedding API 时报错
    },
    # Doc Editor
    "workspace": None,  # None → 用当前工作目录; 字符串 → 用该绝对/相对路径
}


def load_config(persist: bool = False) -> Dict:
    """加载配置;文件不存在或损坏 → 返回默认。永不报错,永不阻塞启动。

    persist=True(显式调用时):.env 带来的值【自动落盘】到全局 config.json,
    用于保留跨项目配置；默认只读，不自动写入配置文件。
    persist=False(默认,给测试/非入口用):只读不写。
    """
    load_dotenv()
    data: Dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Agent configuration could not be read; using defaults")
            data = {}
    cfg = _merge_defaults(data)
    changed = _apply_env_overrides(cfg)
    if persist and changed:
        try:
            save_config(cfg)
        except Exception:
            pass     # 落盘失败不阻塞启动(只读目录/磁盘满等),env 值本轮仍生效
    return cfg


def _merge_defaults(data: Dict) -> Dict:
    out = {**DEFAULT_CONFIG, **data}
    out["memory"] = {**DEFAULT_CONFIG["memory"], **(data.get("memory") or {})}
    return out


def _apply_env_overrides(cfg: Dict) -> bool:
    """.env 里有 LLM_API_KEY / LLM_MODEL_ID / LLM_BASE_URL 就【覆盖】cfg(env 优先)。
    返回是否改动了 cfg —— 仅在真有变化时才需落盘(幂等,不每次启动空写)。"""
    changed = False
    for cfg_key, env_key in (("api_key", "LLM_API_KEY"),
                              ("model",   "LLM_MODEL_ID"),
                              ("base_url","LLM_BASE_URL")):
        env_val = (os.getenv(env_key) or "").strip().strip('"').strip("'")
        if env_val and cfg.get(cfg_key) != env_val:
            cfg[cfg_key] = env_val
            changed = True
    return changed


def save_config(cfg: Dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


def is_ready(cfg: Dict) -> bool:
    return bool(cfg.get("api_key") and cfg.get("model") and cfg.get("provider"))


def _resolve_storage_dirs(base: Path, cwd: Path):
    """返回 (项目层 storage_dir, 用户层 storage_dir, project_id)。
    项目根 = cwd 向上最近 .git 祖先,找不到用 cwd。"""
    from knowpath_backend.memory.project_root import (
        resolve_project_root, project_storage_dir, user_storage_dir, project_id,
    )
    root = resolve_project_root(cwd)
    return project_storage_dir(base, root), user_storage_dir(base), project_id(root)


def build_agent(cfg: Dict) -> Optional[MyFunctionCallAgent]:
    """根据 cfg 构建 agent。配置不全或构造失败 → 返回 None(不抛异常)。"""
    if not is_ready(cfg):
        return None

    try:
        llm = MyLLM(
            provider=cfg["provider"],
            model=cfg["model"],
            api_key=cfg["api_key"],
            base_url=cfg.get("base_url"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
        )
    except Exception as exc:
        logger.error("Agent model initialization failed")
        return None

    base_dir = Path(os.getenv("MY_CHAT_STORAGE", str(DEFAULT_STORAGE_DIR)))
    storage_dir, user_dir, pid = _resolve_storage_dirs(base_dir, Path.cwd())
    logger.debug("Agent memory storage: %s", storage_dir)
    mem_cfg_data = cfg["memory"]
    memory_config = MemoryConfig(
        storage_dir=storage_dir,
        user_storage_dir=user_dir,
        project_id=pid,
        cold_backend=mem_cfg_data["cold_backend"],
        vector_backend=mem_cfg_data["vector_backend"],
        l1_max_tokens=4000,
        l1_recent_turns=6,
        promote_threshold=0.6,
        promote_min_hits=3,
        tick_mode=mem_cfg_data["tick_mode"],
        tick_every_n_turns=3,
        conflict_strength=mem_cfg_data["conflict_strength"],
    )

    # ── Doc Editor: 就地工作区 + 4 个文件工具 ────────────────────
    from knowpath_backend.workspace import Workspace
    from knowpath_backend.tools.builtin.read_file import ReadFile
    from knowpath_backend.tools.builtin.edit_file import EditFile
    from knowpath_backend.tools.builtin.write_file import WriteFile
    from knowpath_backend.tools.builtin.list_dir import ListDir
    from knowpath_backend.tools.builtin.grep import GrepTool
    from knowpath_backend.tools.builtin.glob import GlobTool
    from knowpath_backend.tools.builtin.bash import BashTool

    try:
        ws = Workspace(cfg.get("workspace"))
    except (FileNotFoundError, NotADirectoryError) as exc:
        logger.error("Agent workspace initialization failed")
        return None
    logger.debug("Agent workspace: %s", ws.root)

    registry = ToolRegistry()
    registry.register_tool(CalculatorTool())
    registry.register_tool(ReadFile(ws))
    registry.register_tool(EditFile(ws))
    registry.register_tool(WriteFile(ws))
    registry.register_tool(ListDir(ws))
    registry.register_tool(GrepTool(ws))
    registry.register_tool(GlobTool(ws))
    registry.register_tool(BashTool(ws))

    from knowpath_backend.planning.todo import TodoStore, WriteTodoTool
    todo_store = TodoStore()
    registry.register_tool(WriteTodoTool(todo_store))

    try:
        agent = MyFunctionCallAgent(
            name="伙伴",
            llm=llm,
            tool_registry=registry,
            system_prompt=(
                "你是 lk_hhh 的长期 AI 伙伴。你会记住所有重要对话,"
                "并在用户的偏好/事实变化时主动更新记忆。"
                "用自然、温暖但不啰嗦的语气。\n"
                "闲聊、问候、简单问题就直接简短回应。**不要主动浏览工作区,"
                "也不要在没被要求时调用文件工具(LS/Read/Edit/Write)**——"
                "只有当用户明确要求看/改文件、或任务确实需要时才动文件工具。\n\n"
                "## 文件操作协议\n"
                "当前目录就是你的工作区,需要动文件时直接对真实文件操作,无需拷贝:\n"
                "1. 用 Read / LS 查看文件;Read/LS 可读工作区以外的路径"
                "(如依赖库、系统配置),用于理解上下文。\n"
                "1.5 探索代码先用 Glob 按模式找文件、Grep 搜内容定位(返回行号),"
                "再用 Read 精读那几行(offset/limit),不要整文件全读;"
                "不要读 .svg/图片等二进制原始内容;需要读多个文件时在一条消息里并行发多个 Read。\n"
                "2. 用 Edit / Write 修改文件 —— 只能写当前工作区(启动目录)子树内;"
                "写工作区以外会被拒绝。\n"
                "3. Edit / Write 调用时框架通过审批回调向调用方请求授权(提供 diff)。"
                "你不用追问'要不要确认',直接调即可。用户若拒绝,你会收到"
                " \"用户拒绝了对 X 的调用\",请结合上下文道歉/改方案/继续聊。\n"
                "\n## 任务规划\n"
                "**什么时候用 write_todo**:只有【需要动手的复杂多步任务】"
                "(多文件 / 多步骤 / 易遗漏)才用,先列出分步计划。\n"
                "**什么时候不用**:闲聊、问答、纯解释、单步任务、以及【只读分析】"
                "(即便要看很多文件)——一律直接做,不要用 write_todo。\n"
                "**执行纪律**:同一时刻【只能有一个步骤是 in_progress】(就是你正在做的那步,"
                "会被高亮);每做完一步,**立刻**再调一次 write_todo 把那步标 completed、"
                "下一步标 in_progress,然后才继续——不要等全部做完才一次性更新,"
                "用户要靠这个看实时打勾。每次调用都要传【完整清单】(覆盖式更新)。\n"
            ),
            memory_config=memory_config,
            max_steps=1000,
            workspace=ws,
            enable_verify=True,
            enable_tdd=os.getenv("MY_ENABLE_TDD", "1") != "0",
            todo_store=todo_store,
        )
    except Exception as exc:
        logger.error("Agent initialization failed")
        return None

    # 升级 memory(embedding + summarizer + KG 等高级能力)
    original_memory = getattr(agent, "memory", None)
    if mem_cfg_data["use_embedding"]:
        try:
            agent.memory = MemoryManager(
                memory_config,
                embedding=OpenAIEmbedding.from_llm(llm),
                summarizer=LLMSummarizer(llm, max_tokens=400),
                llm=llm,
            )
        except Exception as exc:
            logger.warning("Agent embedding initialization failed; using TF-IDF")
            agent.memory = MemoryManager(
                memory_config,
                summarizer=LLMSummarizer(llm, max_tokens=400),
                llm=llm,
            )
    else:
        agent.memory = MemoryManager(
            memory_config,
            summarizer=LLMSummarizer(llm, max_tokens=400),
            llm=llm,
        )

    if original_memory is not None:
        original_memory.close()
    return agent
