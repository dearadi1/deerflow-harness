"""统一嵌入式管理接口的配置来源，避免调用方工作目录改变默认配置。"""

import os
from pathlib import Path

from deerflow.config.app_config import AppConfig, reload_app_config


def load_client_config(config_path: str | None = None) -> AppConfig:
    """默认使用包所在项目的 config.yaml，显式相对路径按调用方工作目录解析。

    配置为进程级共享状态，同一进程的管理接口应使用同一个配置文件。
    """
    path = Path(config_path) if config_path is not None else Path(__file__).resolve().parent.parent / "config.yaml"
    resolved_path = str(path.resolve())
    config = reload_app_config(resolved_path)
    # 后续 get_app_config 会重新解析路径，必须固定来源以保留配置及热更新语义。
    os.environ["DEER_FLOW_CONFIG_PATH"] = resolved_path
    return config
