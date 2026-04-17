import tempfile
import unittest
from unittest.mock import patch

from src.core.config import config
from src.core.database import Database
from src.services.captcha_api_service import (
    ApiCaptchaSolution,
    CaptchaProviderError,
    build_captcha_task_plan,
    parse_provider_fallback_order,
    resolve_enterprise_enabled,
    solve_with_provider,
)
from src.services.flow_client import FlowClient


class GenerationConfigMaxRetriesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        self._original_image_timeout = config.image_timeout
        self._original_video_timeout = config.video_timeout
        self._original_max_retries = config.flow_max_retries
        self._orig_captcha_method = config.captcha_method
        self._orig_enterprise_mode = config.captcha_enterprise_mode
        self._orig_retry_eval = config.captcha_api_retry_on_evaluation_failed
        self._orig_fallback = config.captcha_provider_fallback_order
        await self.db.init_db()

    async def asyncTearDown(self):
        config.set_image_timeout(self._original_image_timeout)
        config.set_video_timeout(self._original_video_timeout)
        config.set_flow_max_retries(self._original_max_retries)
        config.set_captcha_method(self._orig_captcha_method)
        config.set_captcha_enterprise_mode(self._orig_enterprise_mode)
        config.set_captcha_api_retry_on_evaluation_failed(self._orig_retry_eval)
        config.set_captcha_provider_fallback_order(self._orig_fallback)
        self._temp_dir.cleanup()

    async def test_init_config_from_toml_persists_flow_max_retries(self):
        await self.db.init_config_from_toml(
            {
                "generation": {
                    "image_timeout": 321,
                    "video_timeout": 654,
                },
                "flow": {
                    "max_retries": 7,
                },
            },
            is_first_startup=True,
        )

        generation_config = await self.db.get_generation_config()

        self.assertIsNotNone(generation_config)
        self.assertEqual(generation_config.image_timeout, 321)
        self.assertEqual(generation_config.video_timeout, 654)
        self.assertEqual(generation_config.max_retries, 7)

    async def test_reload_config_to_memory_syncs_max_retries(self):
        await self.db.init_config_from_toml(
            {
                "generation": {
                    "image_timeout": 300,
                    "video_timeout": 1500,
                },
                "flow": {
                    "max_retries": 3,
                },
            },
            is_first_startup=True,
        )

        await self.db.update_generation_config(max_retries=9)
        await self.db.reload_config_to_memory()

        self.assertEqual(config.flow_max_retries, 9)

    def test_parse_provider_fallback_order_explicit_yescaptcha_only(self):
        order = parse_provider_fallback_order(
            "yescaptcha",
            primary="yescaptcha",
            prepend_primary=False,
        )
        self.assertEqual(order, ["yescaptcha"])

    def test_parse_provider_fallback_order_explicit_yescaptcha_capsolver_only(self):
        order = parse_provider_fallback_order(
            "yescaptcha,capsolver",
            primary="yescaptcha",
            prepend_primary=False,
        )
        self.assertEqual(order, ["yescaptcha", "capsolver"])

    def test_parse_provider_fallback_order_ignores_unknown_and_dedupes(self):
        order = parse_provider_fallback_order(
            "yescaptcha,unknown,yescaptcha,capsolver",
            primary="yescaptcha",
            prepend_primary=False,
        )
        self.assertEqual(order, ["yescaptcha", "capsolver"])

    def test_parse_provider_fallback_order_legacy_blank_still_expands(self):
        order = parse_provider_fallback_order("", primary="yescaptcha", prepend_primary=True)
        self.assertEqual(order[0], "yescaptcha")
        for provider in ("yescaptcha", "capsolver", "capmonster", "ezcaptcha"):
            self.assertIn(provider, order)

    def test_enterprise_mode_selection(self):
        self.assertTrue(resolve_enterprise_enabled("force_on", False))
        self.assertFalse(resolve_enterprise_enabled("force_off", True))
        self.assertTrue(resolve_enterprise_enabled("auto", True))
        self.assertFalse(resolve_enterprise_enabled("auto", False))

    def test_yescaptcha_enterprise_task_default(self):
        original_mode = config.captcha_enterprise_mode
        original_override = config.yescaptcha_task_type_override
        original_key = config.yescaptcha_api_key
        try:
            config.set_captcha_enterprise_mode("auto")
            config.set_yescaptcha_task_type_override("")
            config.set_yescaptcha_api_key("dummy-key")
            plan = build_captcha_task_plan(
                provider="yescaptcha",
                website_url="https://labs.google/fx/tools/flow/project/pid",
                enterprise_required=True,
                action="IMAGE_GENERATION",
            )
            self.assertEqual(plan.task_type, "RecaptchaV3EnterpriseTask")
        finally:
            config.set_captcha_enterprise_mode(original_mode)
            config.set_yescaptcha_task_type_override(original_override)
            config.set_yescaptcha_api_key(original_key)

    def test_yescaptcha_enterprise_override_wins(self):
        original_mode = config.captcha_enterprise_mode
        original_override = config.yescaptcha_task_type_override
        original_key = config.yescaptcha_api_key
        try:
            config.set_captcha_enterprise_mode("auto")
            config.set_yescaptcha_task_type_override("RecaptchaV3EnterpriseTaskM1")
            config.set_yescaptcha_api_key("dummy-key")
            plan = build_captcha_task_plan(
                provider="yescaptcha",
                website_url="https://labs.google/fx/tools/flow/project/pid",
                enterprise_required=True,
                action="IMAGE_GENERATION",
            )
            self.assertEqual(plan.task_type, "RecaptchaV3EnterpriseTaskM1")
        finally:
            config.set_captcha_enterprise_mode(original_mode)
            config.set_yescaptcha_task_type_override(original_override)
            config.set_yescaptcha_api_key(original_key)

    async def test_reload_config_backwards_compatible_for_captcha_strategy(self):
        await self.db.update_captcha_config(captcha_method="yescaptcha")
        await self.db.reload_config_to_memory()

        self.assertIn(config.captcha_enterprise_mode, {"auto", "force_on", "force_off"})
        self.assertIsInstance(config.captcha_api_retry_on_evaluation_failed, bool)

    async def test_retry_on_evaluation_failed_does_not_advance_with_yescaptcha_only(self):
        client = FlowClient(proxy_manager=None, db=self.db)
        config.set_captcha_method("yescaptcha")
        config.set_captcha_provider_fallback_order("yescaptcha")
        config.set_captcha_api_retry_on_evaluation_failed(True)

        current = client._get_current_api_provider("yescaptcha", "project-1", "IMAGE_GENERATION")
        self.assertEqual(current, "yescaptcha")

        should_retry = await client._handle_retryable_generation_error(
            error=Exception("PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"),
            retry_attempt=0,
            max_retries=2,
            browser_id=None,
            project_id="project-1",
            log_prefix="[TEST]",
        )
        self.assertTrue(should_retry)
        current_after = client._get_current_api_provider("yescaptcha", "project-1", "IMAGE_GENERATION")
        self.assertEqual(current_after, "yescaptcha")

    async def test_retry_on_evaluation_failed_advances_provider_with_yescaptcha_capsolver(self):
        client = FlowClient(proxy_manager=None, db=self.db)
        config.set_captcha_method("yescaptcha")
        config.set_captcha_provider_fallback_order("yescaptcha,capsolver")
        config.set_captcha_api_retry_on_evaluation_failed(True)

        current = client._get_current_api_provider("yescaptcha", "project-1", "IMAGE_GENERATION")
        self.assertEqual(current, "yescaptcha")

        should_retry = await client._handle_retryable_generation_error(
            error=Exception("PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"),
            retry_attempt=0,
            max_retries=2,
            browser_id=None,
            project_id="project-1",
            log_prefix="[TEST]",
        )
        self.assertTrue(should_retry)
        current_after = client._get_current_api_provider("yescaptcha", "project-1", "IMAGE_GENERATION")
        self.assertEqual(current_after, "capsolver")

    async def test_final_provider_error_is_preserved(self):
        client = FlowClient(proxy_manager=None, db=self.db)
        error = CaptchaProviderError(
            "provider_unsupported_enterprise: yescaptcha enterprise mode is not reliable for this Flow target.",
            code="provider_unsupported_enterprise",
            provider="yescaptcha",
        )
        client._set_last_api_captcha_error(error, provider="yescaptcha")
        built = client._build_recaptcha_failure_exception()
        self.assertIn("provider_unsupported_enterprise", str(built))

    async def test_api_captcha_solution_preserves_user_agent(self):
        original_key = config.yescaptcha_api_key
        try:
            config.set_yescaptcha_api_key("dummy-key")

            class _FakeResponse:
                def __init__(self, payload):
                    self.status_code = 200
                    self.headers = {"content-type": "application/json"}
                    self._payload = payload

                async def text(self):
                    return self._payload

            class _FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def post(self, url, json=None, timeout=None):
                    if url.endswith("/createTask"):
                        return _FakeResponse('{"errorId":0,"taskId":12345,"status":"processing"}')
                    return _FakeResponse(
                        '{"errorId":0,"status":"ready","solution":{"gRecaptchaResponse":"token-abc","userAgent":"Mozilla/5.0 Test UA"}}'
                    )

            with patch("src.services.captcha_api_service.AsyncSession", return_value=_FakeSession()):
                solution = await solve_with_provider(
                    provider="yescaptcha",
                    website_url="https://labs.google/fx/tools/flow/project/project-1",
                    website_key="site-key",
                    action="IMAGE_GENERATION",
                    enterprise_required=True,
                    project_id="project-1",
                )
            self.assertEqual(solution.token, "token-abc")
            self.assertEqual(solution.user_agent, "Mozilla/5.0 Test UA")
            self.assertIn("userAgent", solution.solution_keys)
            self.assertIn("gRecaptchaResponse", solution.solution_keys)
        finally:
            config.set_yescaptcha_api_key(original_key)

    def test_apply_api_captcha_submission_fingerprint_uses_provider_user_agent(self):
        client = FlowClient(proxy_manager=None, db=self.db)
        client._set_last_api_captcha_solution(
            ApiCaptchaSolution(token="token-abc", user_agent="Mozilla/5.0 Provider UA", solution_keys=("gRecaptchaResponse", "userAgent"))
        )
        headers = {
            "User-Agent": "Mozilla/5.0 Desktop UA",
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": "\"Android\"",
            "x-client-data": "test-client-data",
        }
        result = client._apply_api_captcha_submission_fingerprint(headers)
        self.assertEqual(result.get("User-Agent"), "Mozilla/5.0 Provider UA")
        self.assertNotIn("sec-ch-ua-mobile", result)
        self.assertNotIn("sec-ch-ua-platform", result)
        self.assertNotIn("x-client-data", result)

    def test_apply_api_captcha_submission_fingerprint_without_provider_user_agent_strips_conflicting_hints(self):
        client = FlowClient(proxy_manager=None, db=self.db)
        client._set_last_api_captcha_solution(
            ApiCaptchaSolution(token="token-abc", user_agent=None, solution_keys=("gRecaptchaResponse",))
        )
        headers = {
            "User-Agent": "Mozilla/5.0 Desktop UA",
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": "\"Android\"",
            "x-client-data": "test-client-data",
        }
        result = client._apply_api_captcha_submission_fingerprint(headers)
        self.assertEqual(result.get("User-Agent"), "Mozilla/5.0 Desktop UA")
        self.assertNotIn("sec-ch-ua-mobile", result)
        self.assertNotIn("sec-ch-ua-platform", result)
        self.assertNotIn("x-client-data", result)

    def test_request_body_redacts_recaptcha_token(self):
        payload = {
            "clientContext": {
                "recaptchaContext": {
                    "token": "raw-token-value"
                }
            }
        }
        redacted = FlowClient._redact_recaptcha_token_body(payload)
        self.assertEqual(redacted["clientContext"]["recaptchaContext"]["token"], "<redacted token len=15>")
        self.assertNotIn("raw-token-value", str(redacted))

    def test_proxy_logging_redacts_credentials(self):
        proxy = "http://user:pass@example.com:8080"
        redacted = FlowClient._sanitize_proxy_for_log(proxy)
        self.assertEqual(redacted, "http://<redacted>@example.com:8080")
        self.assertNotIn("user:pass", redacted)


if __name__ == "__main__":
    unittest.main()
