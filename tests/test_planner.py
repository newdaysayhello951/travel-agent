import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('LLM_PROVIDER', 'openai_compatible')
os.environ.setdefault('LLM_BASE_URL', 'https://example.invalid/v1')
os.environ.setdefault('LLM_API_KEY', 'test-only')
os.environ.setdefault('LLM_MODEL', 'test-only')
from agents.planner import TripPlanner
from render import parse_plan


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_tools_deduplicated_and_build_idempotent(self):
        planner = TripPlanner(object())
        poi = SimpleNamespace(name='maps_text_search')
        weather = SimpleNamespace(name='maps_weather')
        planner.mcp = SimpleNamespace(get_tools_for=AsyncMock(side_effect=[[poi], [weather], [poi]]))
        with patch('agents.planner.create_agent', return_value=object()) as factory:
            await planner.build()
            await planner.build()
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(factory.call_args.kwargs['tools'], [poi, weather])

    async def test_missing_tools_fails_explicitly(self):
        planner = TripPlanner(object())
        planner.mcp = SimpleNamespace(get_tools_for=AsyncMock(return_value=[]))
        with self.assertRaises(RuntimeError):
            await planner.build()

    async def test_stream_preserves_spaces_and_reports_tool_progress(self):
        async def events(*a, **kw):
            yield {'event':'on_tool_start', 'name':'maps_weather'}
            for text in ['{"city":"New', ' ', 'York","days":[]}']:
                yield {'event':'on_chat_model_stream', 'data':{'chunk':SimpleNamespace(content=text)}}
        planner = TripPlanner(object())
        planner._agent = SimpleNamespace(astream_events=events)
        with patch('agents.planner.validate_plan_text'):
            result = ''.join([chunk async for chunk in planner.stream('test')])
        self.assertIn('New York', result)
        self.assertIn('查询天气', result)


if __name__ == '__main__':
    unittest.main()
