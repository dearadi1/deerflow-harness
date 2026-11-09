"""与会话、技能和线程管理器同级的环境变量管理入口。"""

from __future__ import annotations

from deerflow.client_config import load_client_config
from deerflow.environment import EnvironmentStore


class _PublicEnvironmentClient:
    def __init__(self, store: EnvironmentStore) -> None:
        self._store = store

    def list_variables(self) -> dict[str, str]:
        return self._store.read()

    def get_variable(self, name: str) -> str:
        return self.list_variables()[name]

    def create_variable(self, name: str, value: str) -> None:
        self._store.mutate(None, name, value, operation="create")

    def update_variable(self, name: str, value: str) -> None:
        self._store.mutate(None, name, value, operation="update")

    def delete_variable(self, name: str) -> None:
        self._store.mutate(None, name, None, operation="delete")


class DeerFlowEnvironmentClient:
    """公共操作通过 public 访问；私有操作必须显式提供存储 user_id。"""

    def __init__(self, config_path: str | None = None) -> None:
        load_client_config(config_path)
        self._store = EnvironmentStore()
        self._public = _PublicEnvironmentClient(self._store)

    @property
    def public(self) -> _PublicEnvironmentClient:
        return self._public

    @staticmethod
    def _require_user_id(user_id: str) -> str:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id 必须是非空字符串")
        return user_id

    def list_variables(self, user_id: str) -> dict[str, str]:
        return self._store.read(self._require_user_id(user_id))

    def get_variable(self, user_id: str, name: str) -> str:
        return self.list_variables(user_id)[name]

    def get_effective_variables(self, user_id: str) -> dict[str, str]:
        return self._store.effective(self._require_user_id(user_id))

    def create_variable(self, user_id: str, name: str, value: str) -> None:
        self._store.mutate(self._require_user_id(user_id), name, value, operation="create")

    def update_variable(self, user_id: str, name: str, value: str) -> None:
        self._store.mutate(self._require_user_id(user_id), name, value, operation="update")

    def delete_variable(self, user_id: str, name: str) -> None:
        self._store.mutate(self._require_user_id(user_id), name, None, operation="delete")
