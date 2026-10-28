"""统一嵌入式管理接口的配置注入，避免隐式查找配置文件。"""

import os
from pathlib import Path

from deerflow.config.app_config import AppConfig, reload_app_config


def load_client_config(config_path: str | None = None) -> AppConfig:
    """显式路径优先，否则读取 DEERFLOW_CONFIG_PATH；相对路径按工作目录解析。

    配置为进程级共享状态，同一进程的管理接口应使用同一个配置文件。
    """
    selected_path = config_path if config_path is not None else os.environ.get("DEERFLOW_CONFIG_PATH")
    if not selected_path:
        raise ValueError("必须显式传入 config_path 或设置环境变量 DEERFLOW_CONFIG_PATH")
    resolved_path = str(Path(selected_path).resolve())
    config = reload_app_config(resolved_path)
    # 后续 get_app_config 会重新解析路径，必须固定来源以保留配置及热更新语义。
    os.environ["DEER_FLOW_CONFIG_PATH"] = resolved_path
    return config
