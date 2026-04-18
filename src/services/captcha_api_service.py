"""Shared API captcha provider utilities and solver."""
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from ..core.config import config
from ..core.logger import debug_logger

SUPPORTED_API_CAPTCHA_METHODS = ("yescaptcha", "capmonster", "ezcaptcha", "capsolver")
ENTERPRISE_MODES = {"auto", "force_on", "force_off"}


class CaptchaProviderError(RuntimeError):
    """Structured provider failure."""

    def __init__(
        self,
        message: str,
        code: str = "provider_error",
        provider: Optional[str] = None,
        detail: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.detail = detail or message


@dataclass
class CaptchaTaskPlan:
    provider: str
    client_key: str
    base_url: str
    task_type: str
    enterprise_enabled: bool
    enterprise_mode: str
    unsupported_reason: Optional[str] = None


@dataclass
class ApiCaptchaSolution:
    token: str
    user_agent: Optional[str] = None
    solution_keys: Tuple[str, ...] = ()


def parse_provider_fallback_order(
    raw_order: str,
    primary: Optional[str] = None,
    prepend_primary: bool = False,
) -> List[str]:
    providers: List[str] = []
    seen = set()

    for item in (raw_order or "").split(","):
        normalized = item.strip().lower()
        if normalized in SUPPORTED_API_CAPTCHA_METHODS and normalized not in seen:
            providers.append(normalized)
            seen.add(normalized)

    if providers:
        if primary and primary in SUPPORTED_API_CAPTCHA_METHODS and prepend_primary:
            if primary in providers:
                providers.remove(primary)
            providers.insert(0, primary)
        return providers

    if primary and primary in SUPPORTED_API_CAPTCHA_METHODS and prepend_primary:
        providers.append(primary)
        seen.add(primary)

    for provider in SUPPORTED_API_CAPTCHA_METHODS:
        if provider not in seen:
            providers.append(provider)

    return providers


def resolve_enterprise_enabled(mode: str, enterprise_required: bool) -> bool:
    normalized_mode = (mode or "auto").strip().lower()
    if normalized_mode not in ENTERPRISE_MODES:
        normalized_mode = "auto"

    if normalized_mode == "force_on":
        return True
    if normalized_mode == "force_off":
        return False
    return bool(enterprise_required)


def _provider_credentials(provider: str) -> tuple[str, str]:
    if provider == "yescaptcha":
        return config.yescaptcha_api_key, config.yescaptcha_base_url
    if provider == "capmonster":
        return config.capmonster_api_key, config.capmonster_base_url
    if provider == "ezcaptcha":
        return config.ezcaptcha_api_key, config.ezcaptcha_base_url
    if provider == "capsolver":
        return config.capsolver_api_key, config.capsolver_base_url
    return "", ""


def parse_proxy_for_captcha_task(proxy_url: str) -> Optional[Dict[str, Any]]:
    """Parse proxy URL into captcha task fields."""
    if not proxy_url:
        return None
    try:
        parsed = urlparse(proxy_url.strip())
        proxy_type = "socks5" if (parsed.scheme or "").startswith("socks5") else "http"
        if not parsed.hostname or not parsed.port:
            return None

        proxy_task: Dict[str, Any] = {
            "proxyType": proxy_type,
            "proxyAddress": parsed.hostname,
            "proxyPort": parsed.port,
        }
        if parsed.username:
            proxy_task["proxyLogin"] = parsed.username
        if parsed.password:
            proxy_task["proxyPassword"] = parsed.password
        return proxy_task
    except Exception:
        return None


def build_captcha_task_plan(
    provider: str,
    website_url: str,
    enterprise_required: bool,
    action: str,
    use_proxy: bool = False,
) -> CaptchaTaskPlan:
    provider = (provider or "").strip().lower()
    if provider not in SUPPORTED_API_CAPTCHA_METHODS:
        raise CaptchaProviderError(
            f"不支持的打码方式: {provider}",
            code="provider_error",
            provider=provider,
        )

    client_key, base_url = _provider_credentials(provider)
    if not client_key:
        raise CaptchaProviderError(
            f"{provider} API Key 未配置",
            code="provider_key_missing",
            provider=provider,
        )

    enterprise_mode = (config.captcha_enterprise_mode or "auto").strip().lower()
    enterprise_enabled = resolve_enterprise_enabled(enterprise_mode, enterprise_required)

    task_type = ""
    unsupported_reason = None
    yescaptcha_override = (config.yescaptcha_task_type_override or "").strip()

    if provider == "yescaptcha":
        if yescaptcha_override:
            task_type = yescaptcha_override
        elif enterprise_enabled:
            task_type = "RecaptchaV3EnterpriseTask"
        else:
            task_type = "RecaptchaV3TaskProxylessM1"
    elif provider == "capmonster":
        task_type = "RecaptchaV3Task" if use_proxy else "RecaptchaV3TaskProxyless"
    elif provider == "ezcaptcha":
        task_type = "ReCaptchaV3TaskS9" if use_proxy else "ReCaptchaV3TaskProxylessS9"
    elif provider == "capsolver":
        if enterprise_enabled:
            task_type = "ReCaptchaV3EnterpriseTask" if use_proxy else "ReCaptchaV3EnterpriseTaskProxyLess"
        else:
            task_type = "ReCaptchaV3Task" if use_proxy else "ReCaptchaV3TaskProxyLess"

    plan = CaptchaTaskPlan(
        provider=provider,
        client_key=client_key,
        base_url=(base_url or "").rstrip("/"),
        task_type=task_type,
        enterprise_enabled=enterprise_enabled,
        enterprise_mode=enterprise_mode if enterprise_mode in ENTERPRISE_MODES else "auto",
        unsupported_reason=unsupported_reason,
    )

    debug_logger.log_info(
        f"[reCAPTCHA] provider={plan.provider}, task_type={plan.task_type}, "
        f"enterprise_mode={plan.enterprise_mode}, enterprise={plan.enterprise_enabled}, "
        f"action={action}, website={website_url}, override_used={bool(yescaptcha_override)}"
    )
    return plan


async def _read_response_debug_payload(resp) -> dict:
    text_reader = getattr(resp, "text", None)
    text = ""
    if callable(text_reader):
        text_result = text_reader()
        if asyncio.iscoroutine(text_result):
            text = await text_result
        else:
            text = text_result or ""
    elif isinstance(text_reader, str):
        text = text_reader

    status = getattr(resp, "status_code", None)
    if status is None:
        status = getattr(resp, "status", None)

    headers = getattr(resp, "headers", {}) or {}
    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    snippet = (text or "")[:500]
    payload = {
        "status": status,
        "content_type": content_type,
        "text": text,
        "snippet": snippet,
        "is_json": "application/json" in content_type.lower(),
    }
    return payload


def _safe_parse_json_from_text(text: str):
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _sanitize_provider_text_for_log(text: str) -> str:
    parsed = _safe_parse_json_from_text(text)
    if not isinstance(parsed, dict):
        return text

    solution = parsed.get("solution")
    if isinstance(solution, dict):
        token = solution.get("gRecaptchaResponse") or solution.get("token")
        if isinstance(token, str) and token:
            redacted = f"<redacted token len={len(token)}>"
            if "gRecaptchaResponse" in solution:
                solution["gRecaptchaResponse"] = redacted
            if "token" in solution:
                solution["token"] = redacted
    try:
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return text


async def solve_with_provider(
    provider: str,
    website_url: str,
    website_key: str,
    action: str,
    enterprise_required: bool,
    project_id: Optional[str] = None,
    has_fingerprint_context: bool = False,
    using_submission_proxy: bool = False,
    submission_proxy_url: Optional[str] = None,
) -> ApiCaptchaSolution:
    use_proxy = bool(submission_proxy_url)
    plan = build_captcha_task_plan(
        provider=provider,
        website_url=website_url,
        enterprise_required=enterprise_required,
        action=action,
        use_proxy=use_proxy,
    )

    if plan.unsupported_reason:
        raise CaptchaProviderError(
            plan.unsupported_reason,
            code="provider_unsupported_enterprise",
            provider=plan.provider,
        )

    task: Dict[str, Any] = {
        "websiteURL": website_url,
        "websiteKey": website_key,
        "type": plan.task_type,
        "pageAction": action,
    }
    if plan.provider == "capsolver" and plan.enterprise_enabled:
        task["isEnterprise"] = True

    if submission_proxy_url:
        proxy_fields = parse_proxy_for_captcha_task(submission_proxy_url)
        if proxy_fields:
            task.update(proxy_fields)
            debug_logger.log_info(
                f"[reCAPTCHA {provider}] using submission proxy for solve: "
                f"proxyType={proxy_fields['proxyType']} proxyAddress={proxy_fields['proxyAddress']}"
            )
        else:
            debug_logger.log_warning(
                f"[reCAPTCHA {provider}] submission_proxy_url provided but could not be parsed, "
                f"falling back to proxyless solve — token may be rejected"
            )

    create_url = f"{plan.base_url}/createTask"
    get_url = f"{plan.base_url}/getTaskResult"

    async with AsyncSession() as session:
        create_resp = await session.post(create_url, json={"clientKey": plan.client_key, "task": task}, timeout=30)
        create_payload = await _read_response_debug_payload(create_resp)
        create_text = create_payload["text"]
        create_content_type = create_payload["content_type"]
        create_snippet = _sanitize_provider_text_for_log(create_payload["snippet"])
        debug_logger.log_info(
            f"[reCAPTCHA {plan.provider}] createTask http_status={create_payload['status']} "
            f"content_type={create_content_type} url={create_url} snippet={create_snippet!r}"
        )

        create_json = _safe_parse_json_from_text(create_text)
        if create_json is None:
            is_empty = not create_text or not create_text.strip()
            error_code = "provider_empty_response" if is_empty else "provider_non_json_response"
            detail = (
                f"status={create_payload['status']}, content_type={create_content_type}, "
                f"snippet={create_snippet!r}"
            )
            raise CaptchaProviderError(
                f"{error_code}: {plan.provider}",
                code=error_code,
                provider=plan.provider,
                detail=detail,
            )

        task_id = create_json.get("taskId")
        debug_logger.log_info(
            f"[reCAPTCHA {plan.provider}] createTask summary: "
            f"errorId={create_json.get('errorId')}, "
            f"taskId={task_id}, "
            f"status={create_json.get('status')}"
        )
        debug_logger.log_info(
            f"[reCAPTCHA {plan.provider}] createTask summary: task_id={task_id}, errorId={create_json.get('errorId')}, "
            f"error={create_json.get('errorDescription') or create_json.get('errorMessage')}, project_id={project_id}, "
            f"fingerprint_ctx={has_fingerprint_context}, submission_proxy={using_submission_proxy}"
        )

        if not task_id:
            error_desc = create_json.get("errorDescription") or create_json.get("errorMessage") or "Unknown error"
            raise CaptchaProviderError(
                f"provider_task_creation_failed: {plan.provider}: {error_desc}",
                code="provider_task_creation_failed",
                provider=plan.provider,
                detail=error_desc,
            )

        poll_errors = 0
        for index in range(40):
            poll_resp = await session.post(get_url, json={"clientKey": plan.client_key, "taskId": task_id}, timeout=30)
            poll_payload = await _read_response_debug_payload(poll_resp)
            poll_text = poll_payload["text"]
            poll_content_type = poll_payload["content_type"]
            poll_snippet = _sanitize_provider_text_for_log(poll_payload["snippet"])
            debug_logger.log_info(
                f"[reCAPTCHA {plan.provider}] poll#{index + 1} http_status={poll_payload['status']} "
                f"content_type={poll_content_type} url={get_url} snippet={poll_snippet!r}"
            )

            poll_json = _safe_parse_json_from_text(poll_text)
            if poll_json is None:
                is_empty = not poll_text or not poll_text.strip()
                error_code = "provider_poll_empty_response" if is_empty else "provider_poll_non_json_response"
                detail = (
                    f"status={poll_payload['status']}, content_type={poll_content_type}, "
                    f"snippet={poll_snippet!r}"
                )
                raise CaptchaProviderError(
                    f"{error_code}: {plan.provider}",
                    code=error_code,
                    provider=plan.provider,
                    detail=detail,
                )

            status = poll_json.get("status")
            solution = poll_json.get("solution") or {}
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            provider_user_agent = solution.get("userAgent")
            solution_keys = list(solution.keys()) if isinstance(solution, dict) else []
            debug_logger.log_info(
                f"[reCAPTCHA {plan.provider}] poll summary: "
                f"errorId={poll_json.get('errorId')}, "
                f"status={status}, "
                f"has_solution={bool(solution)}, "
                f"solution_keys={solution_keys}, "
                f"token_len={len(token) if token else 0}"
            )
            debug_logger.log_info(
                f"[reCAPTCHA {plan.provider}] poll#{index + 1} status={status} errorId={poll_json.get('errorId')}"
            )

            if status == "ready":
                debug_logger.log_info(
                    f"[reCAPTCHA {plan.provider}] ready solution_keys={solution_keys} token_len={len(token) if token else 0}"
                )
                if token:
                    debug_logger.log_info(
                        f"[reCAPTCHA {plan.provider}] token_received=true token_len={len(token)} "
                        f"user_agent_len={len(provider_user_agent) if provider_user_agent else 0}"
                    )
                    return ApiCaptchaSolution(
                        token=token,
                        user_agent=provider_user_agent or None,
                        solution_keys=tuple(solution_keys),
                    )
                raise CaptchaProviderError(
                    f"missing_token: {plan.provider} ready 但未返回 token",
                    code="missing_token",
                    provider=plan.provider,
                )

            if poll_json.get("errorId") not in (None, 0):
                poll_errors += 1
                error_desc = poll_json.get("errorDescription") or poll_json.get("errorMessage") or str(poll_json)
                raise CaptchaProviderError(
                    f"provider_poll_failed: {plan.provider}: {error_desc}",
                    code="provider_poll_failed",
                    provider=plan.provider,
                    detail=error_desc,
                )

            await asyncio.sleep(3)

        raise CaptchaProviderError(
            f"provider_polling_timeout: {plan.provider} task={task_id}",
            code="provider_polling_timeout",
            provider=plan.provider,
        )
