"""为同步客户端外挂最近一轮重放，复用 checkpoint 状态读写与恢复能力。

实现目标是替换最近一次用户输入及其派生对话状态；长期记忆、文件和外部
工具副作用不在恢复范围内。锁仅协调当前进程的客户端调用，跨进程由宿主协调。
"""

from __future__ import annotations

import copy
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END

from deerflow_protocol import ClientStreamEvent

from deerflow.agents.middlewares.message_utils import is_genuine_user_message
from deerflow.runtime.checkpoint_state import CheckpointStateAccessor, build_state_mutation_graph, graph_state_schema
from deerflow.runtime.context_keys import CHECKPOINT_AGENT_NAME_METADATA_KEY, DEFAULT_AGENT_NAME_METADATA_VALUE
from deerflow.utils.thread_id import resolve_thread_id

_OWNER_KEY = "deerflow_embedded_user_id"
_BASE_KEY = "deerflow_embedded_turn_base"
_RUN_KEY = "deerflow_embedded_turn_run_id"
_RETAINED_KEYS = (_OWNER_KEY, _BASE_KEY, _RUN_KEY, CHECKPOINT_AGENT_NAME_METADATA_KEY)
_REGISTRY_GUARD = threading.Lock()
_CLIENT_LOCKS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_THREAD_LOCKS: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
_MAX_LINEAGE_DEPTH = 10000


class ReplayUnavailableError(ValueError):
    """没有可安全重放的最近一轮或缺少完整历史基线。"""


class ReplayConflictError(RuntimeError):
    """客户端或线程仍被另一次生成占用。"""


def _turn_locks(client: Any, thread_id: str) -> tuple[Any, Any]:
    with _REGISTRY_GUARD:
        client_lock = _CLIENT_LOCKS.setdefault(client, threading.Lock())
        thread_lock = _THREAD_LOCKS.get(thread_id)
        if thread_lock is None:
            thread_lock = threading.Lock()
            _THREAD_LOCKS[thread_id] = thread_lock
        return client_lock, thread_lock


@contextmanager
def _reserve_turn(client: Any, thread_id: str) -> Iterator[None]:
    # 拒绝冲突而不阻塞，避免调用方在消费同一个同步流时发生自锁。
    acquired = []
    try:
        for lock in _turn_locks(client, thread_id):
            if not lock.acquire(blocking=False):
                raise ReplayConflictError("客户端或线程正在生成，请完成或关闭原流后重试")
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


def stream_client_turn(client: Any, message: str, *, thread_id: str | None, replay_last_turn: bool, **kwargs: Any) -> Iterator[ClientStreamEvent]:
    """保留原流事件协议，并在异常或提前关闭时恢复重放前的有效状态。"""
    if not isinstance(replay_last_turn, bool):
        raise TypeError("replay_last_turn 必须是布尔值")
    if replay_last_turn and thread_id is None:
        raise ReplayUnavailableError("重新生成必须提供已有 thread_id")
    thread_id = resolve_thread_id(thread_id)
    with _reserve_turn(client, thread_id):
        replay = ClientTurnReplay(enabled=replay_last_turn)
        inner = client._stream_turn(message, thread_id=thread_id, _turn_replay=replay, **kwargs)
        try:
            for event in inner:
                if event.type == "end" and event.agent_id == "main": # 仅识别主agent结束
                    replay.committed = True
                yield event
        finally:
            try:
                inner.close()
            finally:
                replay.restore_if_needed()


def _checkpoint_id(config: dict[str, Any]) -> str | None:
    checkpoint_id = (config.get("configurable") or {}).get("checkpoint_id")
    return checkpoint_id if isinstance(checkpoint_id, str) and checkpoint_id else None


def _metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {key: copy.deepcopy(metadata[key]) for key in _RETAINED_KEYS if metadata and key in metadata}


def _last_user(values: dict[str, Any]) -> Any:
    return next((message for message in reversed(values.get("messages", [])) if is_genuine_user_message(message)), None)


def _contains_turn(values: dict[str, Any], message: Any) -> bool:
    run_id = message.additional_kwargs.get("run_id")
    for candidate in values.get("messages", []):
        if not is_genuine_user_message(candidate):
            continue
        if isinstance(run_id, str) and run_id:
            if candidate.additional_kwargs.get("run_id") == run_id:
                return True
        elif message.id and candidate.id == message.id:
            return True
    return False


@dataclass
class ClientTurnReplay:
    enabled: bool
    committed: bool = False
    restore_required: bool = False
    accessor: CheckpointStateAccessor | None = None
    original_values: dict[str, Any] = field(default_factory=dict)
    original_metadata: dict[str, Any] = field(default_factory=dict)
    pending_writes: tuple = ()
    read_config: dict[str, Any] = field(default_factory=dict)

    def prepare(self, agent: Any, checkpointer: Any, state: dict, config: dict, context: dict, mode: str) -> None:
        """在首次执行前固定基线，避免压缩、注入和连续重生成改变轮次边界。"""
        if checkpointer is None:
            if self.enabled:
                raise ReplayUnavailableError("重新生成需要 checkpointer")
            return
        self.accessor = CheckpointStateAccessor.bind(agent, checkpointer, mode=mode)
        self.read_config = {"configurable": {"thread_id": context["thread_id"], "checkpoint_ns": ""}}
        checkpoint = self.accessor.get_tuple(self.read_config)
        persisted_metadata = getattr(checkpoint, "metadata", None) or {}
        owner = persisted_metadata.get(_OWNER_KEY)
        if owner is not None and owner != context["user_id"]:
            raise PermissionError("当前用户不能访问该线程")
        metadata = config.setdefault("metadata", {})
        metadata.update({
            _OWNER_KEY: context["user_id"],
            _RUN_KEY: context["run_id"],
            _BASE_KEY: _checkpoint_id(checkpoint.config) if checkpoint is not None else "",
            CHECKPOINT_AGENT_NAME_METADATA_KEY: context.get("agent_name") or DEFAULT_AGENT_NAME_METADATA_VALUE,
        })
        if self.enabled:
            self._prepare_replay(state, config, checkpoint, persisted_metadata)

    def _prepare_replay(self, state: dict, config: dict, checkpoint: Any, persisted_metadata: dict) -> None:
        if checkpoint is None:
            raise ReplayUnavailableError("线程尚无可重新生成的对话")
        snapshot = self.accessor.get(self.read_config)
        if snapshot.next or snapshot.tasks:
            raise ReplayConflictError("线程仍有待执行节点，不能重新生成")
        message = _last_user(snapshot.values or {})
        if message is None:
            raise ReplayUnavailableError("线程没有可重新生成的用户输入")
        expected_agent = config["metadata"][CHECKPOINT_AGENT_NAME_METADATA_KEY]
        recorded_agent = persisted_metadata.get(CHECKPOINT_AGENT_NAME_METADATA_KEY)
        if recorded_agent is not None and recorded_agent != expected_agent:
            raise ReplayUnavailableError("请使用上一轮对应的智能体重新生成")
        base_values, base_id = self._find_base(snapshot, message)
        self.original_values = copy.deepcopy(dict(snapshot.values or {}))
        self.original_metadata = _metadata(persisted_metadata)
        self.original_metadata.update({_BASE_KEY: base_id, _RUN_KEY: message.additional_kwargs.get("run_id")})
        self.pending_writes = tuple(copy.deepcopy(getattr(checkpoint, "pending_writes", ()) or ()))
        self._inherit_files(state["messages"][-1], message)
        config["metadata"][_BASE_KEY] = base_id
        self._preserve_goal(base_values)
        # full/delta 都在当前 head 线性替换；旧版本保留在祖先链，避免 delta 分支污染。
        self.restore_required = True
        self._replace(base_values, config["metadata"], "embedded_replay")

    def _preserve_goal(self, base_values: dict) -> None:
        # 保留用户当前设置的目标与预算，但旧答复的达成记录不能认证新答复。
        if "goal" in self.original_values:
            base_values["goal"] = copy.deepcopy(self.original_values["goal"])
        if "goal_outcome" in self.original_values or "goal_outcome" in base_values:
            base_values["goal_outcome"] = None

    @staticmethod
    def _inherit_files(new_message: Any, old_message: Any) -> None:
        # 显式 files=[] 表示移除附件声明；省略参数则复用原轮附件，不复制旧注入文本。
        if "files" not in new_message.additional_kwargs and "files" in old_message.additional_kwargs:
            new_message.additional_kwargs["files"] = copy.deepcopy(old_message.additional_kwargs["files"])

    def _find_base(self, snapshot: Any, message: Any) -> tuple[dict, str]:
        metadata = snapshot.metadata or {}
        run_id = message.additional_kwargs.get("run_id")
        if run_id and metadata.get(_RUN_KEY) == run_id and _BASE_KEY in metadata:
            return self._read_base(metadata[_BASE_KEY], message)
        # 兼容功能上线前的线程，只沿当前父链查找，不会从时间排序误选旧分支。
        seen = set()
        for _ in range(_MAX_LINEAGE_DEPTH):
            checkpoint_id = _checkpoint_id(snapshot.config)
            if checkpoint_id is None or checkpoint_id in seen:
                raise ReplayUnavailableError("checkpoint 父链缺失或存在循环")
            seen.add(checkpoint_id)
            if not _contains_turn(snapshot.values or {}, message):
                return copy.deepcopy(dict(snapshot.values or {})), checkpoint_id
            parent = snapshot.parent_config
            if not parent or _checkpoint_id(parent) is None:
                raise ReplayUnavailableError("无法找到最近一轮之前的 checkpoint")
            self._validate_selector(parent)
            if self.accessor.get_tuple(parent) is None:
                raise ReplayUnavailableError("checkpoint 父节点已丢失")
            snapshot = self.accessor.get(parent)
            if _checkpoint_id(snapshot.config) != _checkpoint_id(parent):
                raise ReplayUnavailableError("checkpoint 父节点无法读取")
        raise ReplayUnavailableError("checkpoint 父链超过安全查询上限")

    def _read_base(self, base_id: Any, message: Any) -> tuple[dict, str]:
        if base_id == "":
            return {}, ""
        if not isinstance(base_id, str):
            raise ReplayUnavailableError("轮次基线标记无效")
        selector = {"configurable": {**self.read_config["configurable"], "checkpoint_id": base_id}}
        if self.accessor.get_tuple(selector) is None:
            raise ReplayUnavailableError("轮次基线 checkpoint 已丢失")
        snapshot = self.accessor.get(selector)
        if _checkpoint_id(snapshot.config) != base_id or _contains_turn(snapshot.values or {}, message):
            raise ReplayUnavailableError("轮次基线丢失或已包含待替换输入")
        return copy.deepcopy(dict(snapshot.values or {})), base_id

    def _validate_selector(self, selector: dict) -> None:
        configurable = selector.get("configurable") or {}
        if configurable.get("thread_id") != self.read_config["configurable"]["thread_id"] or configurable.get("checkpoint_ns", "") != "":
            raise ReplayUnavailableError("checkpoint 父链指向其他线程或命名空间")

    def _replace(self, values: dict, metadata: dict, as_node: str) -> dict:
        # 复用现有 worker 对完整状态及 reducer 的替换规则，保留自定义中间件通道。
        from deerflow.runtime.runs.worker import _complete_state_replacement_values

        current = self._clear_tasks(metadata)
        graph = build_state_mutation_graph(as_node, self.accessor.mode, graph_state_schema(self.accessor.graph))
        replacement = _complete_state_replacement_values(
            mutation_graph=graph,
            selected_values=values,
            current_values=dict(current.values or {}),
            run_id=str(metadata.get(_RUN_KEY) or "embedded_replay"),
            operation=as_node,
        )
        accessor = CheckpointStateAccessor.bind(graph, self.accessor.checkpointer, mode=self.accessor.mode)
        return accessor.update({**self.read_config, "metadata": _metadata(metadata)}, replacement, as_node=as_node)

    def _clear_tasks(self, metadata: dict) -> Any:
        # 消费已完成任务的待提交写入可能再产生下游调度；必须清空整个边界才替换数据。
        current = self.accessor.get(self.read_config)
        for _ in range(100):
            if not current.next and not current.tasks:
                return current
            cleared = self.accessor.update({**self.read_config, "metadata": _metadata(metadata)}, None, as_node=END)
            current = self.accessor.get(cleared)
        raise RuntimeError("无法清理重放遗留任务，已停止恢复")

    def restore_if_needed(self) -> None:
        if not self.restore_required or self.committed:
            return
        restored = self._replace(self.original_values, self.original_metadata, "embedded_replay_restore")
        # 与 worker 回滚相同，pending writes 按原任务恢复，不能复制其他分支的写入。
        writes_by_task: dict[str, list] = {}
        for task_id, channel, value in self.pending_writes:
            writes_by_task.setdefault(task_id, []).append((channel, value))
        for task_id, writes in writes_by_task.items():
            self.accessor.checkpointer.put_writes(restored, writes, task_id=task_id)
        self.restore_required = False
