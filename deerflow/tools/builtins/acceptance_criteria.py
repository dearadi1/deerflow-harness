"""子代理工具共用的验收条件入参类型。"""

import json
from typing import Annotated

from pydantic import BeforeValidator


def _decode_acceptance_criteria(value: object) -> object:
    """兼容模型将数组多序列化一层的输出，保留后续元素类型校验。"""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return value
    return decoded if isinstance(decoded, list) else value


AcceptanceCriteria = Annotated[list[str] | None, BeforeValidator(_decode_acceptance_criteria)]
