"""验证首模型计时覆盖同步／异步真实图，且不把空分片或旁路模型当成首响应。"""

import asyncio
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.callbacks.manager import AsyncCallbackManager, CallbackManager
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from deerflow.tracing.first_model_timing import FirstModelTimingCallback, logger, with_first_model_timing


class FirstModelTimingTests(unittest.TestCase):
    def test_real_graph_logs_first_model_once_on_sync_and_async_streams(self):
        agent = create_agent(FakeListChatModel(responses=["合成回复"]), tools=[])
        state = {"messages": [HumanMessage(content="合成私有输入")]}
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), self.assertLogs(logger, level="INFO") as captured:
                config = with_first_model_timing({})
                if asynchronous:
                    asyncio.run(self.consume_async(agent, state, config))
                else:
                    list(agent.stream(state, config=config, stream_mode="messages"))
            self.assertEqual(len(captured.output), 2)
            self.assertIn("graph_to_model_ms=", captured.output[0])
            self.assertIn("model_to_response_ms=", captured.output[1])
            self.assertIn("kind=chunk", captured.output[1])
            self.assertNotIn("合成", "".join(captured.output))

    async def consume_async(self, agent, state, config):
        async for _ in agent.astream(state, config=config, stream_mode="messages"):
            pass

    def test_empty_chunks_and_other_model_calls_do_not_finish_measurement(self):
        callback = FirstModelTimingCallback()
        main_run, title_run = uuid4(), uuid4()
        with self.assertNoLogs(logger, level="INFO"):
            callback.on_chat_model_start({}, [], run_id=title_run, metadata={"langgraph_node": "title"})
            callback.on_llm_new_token("标题", run_id=title_run)
        with self.assertLogs(logger, level="INFO"):
            callback.on_chat_model_start({}, [], run_id=main_run, metadata={"langgraph_node": "model"})
        with self.assertNoLogs(logger, level="INFO"):
            callback.on_llm_new_token("", run_id=main_run, chunk=ChatGenerationChunk(message=AIMessageChunk(content="")))
            empty_blocks = AIMessageChunk(content=[{"type": "text", "text": ""}, {"type": "thinking", "thinking": ""}])
            callback.on_llm_new_token("", run_id=main_run, chunk=ChatGenerationChunk(message=empty_blocks))
        reasoning = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "合成思考"})
        with self.assertLogs(logger, level="INFO") as captured:
            callback.on_llm_new_token("", run_id=main_run, chunk=ChatGenerationChunk(message=reasoning))
            callback.on_llm_new_token("正文", run_id=main_run)
            callback.on_llm_end(None, run_id=main_run)
        self.assertEqual(len(captured.output), 1)
        self.assertNotIn("合成思考", captured.output[0])

    def test_non_streaming_response_is_explicitly_marked_complete(self):
        callback = FirstModelTimingCallback()
        run_id = uuid4()
        with self.assertLogs(logger, level="INFO") as captured:
            callback.on_chat_model_start({}, [], run_id=run_id, metadata={"langgraph_node": "model"})
            callback.on_llm_end(None, run_id=run_id)
        self.assertIn("kind=complete", captured.output[-1])

    def test_disabled_info_logging_does_not_add_callback_overhead(self):
        config = {"callbacks": []}
        with patch.object(logger, "isEnabledFor", return_value=False):
            self.assertIs(with_first_model_timing(config), config)

    def test_timing_does_not_mutate_existing_callback_managers(self):
        existing = BaseCallbackHandler()
        for callbacks in ([existing], CallbackManager([existing]), AsyncCallbackManager([existing])):
            with self.subTest(callbacks=type(callbacks)), patch.object(logger, "isEnabledFor", return_value=True):
                config = {"callbacks": callbacks}
                timed = with_first_model_timing(config)
            original = callbacks if isinstance(callbacks, list) else callbacks.handlers
            extended = timed["callbacks"] if isinstance(timed["callbacks"], list) else timed["callbacks"].handlers
            self.assertEqual(original, [existing])
            self.assertEqual(len(extended), 2)
            self.assertIs(config["callbacks"], callbacks)


if __name__ == "__main__":
    unittest.main()
