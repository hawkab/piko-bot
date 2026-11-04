"""Standalone regressions for the shared runner's embedding contracts."""

__author__ = "Olshansky Grigory"

import copy
import unittest

from pikobot.agent.hook import AgentHook
from pikobot.agent.runner import AgentRunner, AgentRunSpec
from pikobot.providers.base import ContextWindowExceeded, LLMResponse, ToolCallRequest


class Provider:
    """Return deterministic responses and retain actual request parameters."""

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def chat_with_retry(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class Tools:
    """Record the runner's tool executions without external effects."""

    def __init__(self):
        self.calls = []

    def get_definitions(self):
        return []

    async def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return "tool-result"


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    """Preserve normal runs while supporting durable host-controlled execution."""

    async def test_usage_accumulates_and_hooks_receive_only_current_turn_usage(self):
        usages = []

        class Hook(AgentHook):
            async def after_iteration(self, context):
                usages.append(dict(context.usage))

        provider = Provider(
            LLMResponse(None, [ToolCallRequest("read-1", "read", {})],
                        usage={"prompt_tokens": 7, "completion_tokens": 3}),
            LLMResponse("done", usage={"prompt_tokens": 11, "completion_tokens": 4}),
        )
        tools = Tools()
        result = await AgentRunner(provider).run(AgentRunSpec(
            [{"role": "user", "content": "task"}], tools, "fixture",
            max_iterations=None, hook=Hook(),
        ))
        self.assertEqual(result.final_content, "done")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.usage, {"prompt_tokens": 18, "completion_tokens": 7})
        self.assertEqual(usages, [{"prompt_tokens": 7, "completion_tokens": 3},
                                 {"prompt_tokens": 11, "completion_tokens": 4}])
        self.assertEqual(tools.calls, [("read", {})])
        self.assertTrue(all("max_tokens" not in request for request in provider.requests))

    async def test_terminal_hook_stops_without_another_generation(self):
        class Hook(AgentHook):
            async def after_iteration(self, context):
                context.final_content = "accepted"

            def should_stop(self, context):
                return True

        provider = Provider(LLMResponse(None, [ToolCallRequest("finish-1", "finish", {})]))
        tools = Tools()
        result = await AgentRunner(provider).run(AgentRunSpec(
            [], tools, "fixture", max_iterations=None, hook=Hook(),
        ))
        self.assertEqual(result.final_content, "accepted")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(tools.calls, [("finish", {})])

    async def test_typed_context_rejection_resumes_after_the_hook_checkpoint(self):
        checkpoints = []

        class Hook(AgentHook):
            async def on_context_rejected(self, context, error):
                checkpoints.append(type(error))
                context.messages[:] = [{"role": "user", "content": "checkpoint"}]
                return True

        provider = Provider(ContextWindowExceeded("fixture overflow"), LLMResponse("done"))
        result = await AgentRunner(provider).run(AgentRunSpec(
            [{"role": "system", "content": "fixture"},
             {"role": "user", "content": "long fixture context"}],
            Tools(), "fixture", max_iterations=None, hook=Hook(),
        ))
        self.assertEqual(result.final_content, "done")
        self.assertEqual(checkpoints, [ContextWindowExceeded])
        self.assertEqual([len(r["messages"]) for r in provider.requests], [2, 1])

    async def test_rejected_context_without_a_checkpoint_propagates(self):
        provider = Provider(ContextWindowExceeded("fixture overflow"))
        with self.assertRaises(ContextWindowExceeded):
            await AgentRunner(provider).run(AgentRunSpec(
                [], Tools(), "fixture", max_iterations=None,
            ))
        self.assertEqual(len(provider.requests), 1)

    async def test_unrelated_error_does_not_authorize_context_recovery(self):
        checkpoints = []

        class Hook(AgentHook):
            async def on_context_rejected(self, context, error):
                checkpoints.append(error)
                return True

        provider = Provider(ValueError("unrelated failure"))
        with self.assertRaisesRegex(ValueError, "unrelated failure"):
            await AgentRunner(provider).run(AgentRunSpec(
                [], Tools(), "fixture", max_iterations=None, hook=Hook(),
            ))
        self.assertEqual(checkpoints, [])
        self.assertEqual(len(provider.requests), 1)

    async def test_explicit_iteration_cap_keeps_legacy_behavior(self):
        provider = Provider(LLMResponse(None, [ToolCallRequest("read-1", "read", {})]))
        result = await AgentRunner(provider).run(AgentRunSpec(
            [], Tools(), "fixture", max_iterations=1,
        ))
        self.assertEqual(result.stop_reason, "max_iterations")
        self.assertEqual(len(provider.requests), 1)

    async def test_invalid_iteration_caps_fail_before_inference(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value):
                provider = Provider()
                with self.assertRaises(ValueError):
                    await AgentRunner(provider).run(AgentRunSpec(
                        [], Tools(), "fixture", max_iterations=value,
                    ))
                self.assertEqual(provider.requests, [])
