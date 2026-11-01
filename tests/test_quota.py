import unittest
from quota import is_quota_error


class QuotaTests(unittest.TestCase):
    def test_status_and_suppressed_context_are_detected_without_exposing_body(self):
        error=RuntimeError('private upstream detail');error.status_code=429
        try:
            try:raise error
            except RuntimeError:raise ValueError('safe service error') from None
        except ValueError as wrapped:self.assertTrue(is_quota_error(wrapped))
        self.assertTrue(is_quota_error(ExceptionGroup('group',[error])))
        self.assertFalse(is_quota_error(TimeoutError('network timeout')))


class QuotaRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_error_is_not_retried(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        from reliability import current_budget,ExecutionBudget,call_with_retry,ServiceQuotaError
        error=RuntimeError('upstream');error.status_code=429
        handler=AsyncMock(side_effect=error);token=current_budget.set(ExecutionBudget())
        try:
            with self.assertRaises(ServiceQuotaError):
                await call_with_retry(handler,SimpleNamespace(tool_call={'id':'x','name':'maps_weather','args':{}}),timeout=1,retries=3,max_calls=10)
            self.assertEqual(handler.await_count,1)
        finally:current_budget.reset(token)
