#!/usr/bin/env python3
"""
settings.py — 统一配置管理（pydantic BaseSettings）

将散落在 config.py 顶部的环境变量和代码硬编码常量集中到一个可测试、带类型的配置类。
集成方式：不一次性迁移所有常量，先在 bot.py main() 实例化，逐步替换。
"""
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings


class BotSettings(BaseSettings):
    """Bot 全部配置，支持 .env 文件 + 环境变量自动读取。"""

    # ═══ NapCat 连接 ═══
    napcat_http: str = Field("http://127.0.0.1:3000", validation_alias="NAP_CAT_HTTP")
    napcat_ws: str = Field("ws://127.0.0.1:3001", validation_alias="NAP_CAT_WS")
    my_qq: str = Field("0", validation_alias="MY_QQ")
    self_nick: str = Field("", validation_alias="SELF_NICK")

    # ═══ API Keys ═══
    deepseek_key: str = Field("", validation_alias="DEEPSEEK_API_KEY")
    deepseek_base: str = Field("https://api.deepseek.com", validation_alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field("deepseek-chat", validation_alias="DEEPSEEK_MODEL")
    zhipu_key: str = Field("", validation_alias="ZHIPU_API_KEY")
    zhipu_base: str = Field("https://open.bigmodel.cn/api/paas/v4", validation_alias="ZHIPU_BASE_URL")

    # ═══ 路径 ═══
    data_dir: Path = Field(Path(__file__).parent / "data", validation_alias="DATA_DIR")

    # ═══ 权限 ═══
    master_qq: str = Field("", validation_alias="MASTER_QQ")

    # ═══ 限流（新增可调） ═══
    master_tool_rate: int = Field(30, validation_alias="MASTER_TOOL_RATE")
    group_reply_rate: int = Field(3, validation_alias="GROUP_REPLY_RATE")
    cooldown_seconds: int = Field(6, validation_alias="COOLDOWN_SECONDS")

    # ═══ 容错（新增可调） ═══
    max_retries: int = Field(3, validation_alias="MAX_RETRIES")
    circuit_breaker_threshold: int = Field(5, validation_alias="CIRCUIT_BREAKER_THRESHOLD")
    recovery_timeout: int = Field(60, validation_alias="RECOVERY_TIMEOUT")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # .env 里的自定义字段（如 MCP_TOKEN）不报错


# 全局单例，首次导入时实例化
settings = BotSettings()
