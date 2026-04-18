# Implementation Tracking

## 2026-04-18 — Sticky proxy session pinning for API captcha solve/submit

### Context

Rotating proxies can produce different exit IPs between captcha solving and token submission, which may cause Google to reject submissions due to identity mismatch.

### Changes shipped

- Added sticky proxy URL generation in the captcha API service (`make_sticky_proxy_url`) to append `_session-<id>` when a proxy username is not already sticky.
- Extended `ApiCaptchaSolution` with `sticky_proxy_url` so solve-time proxy identity can be reused downstream.
- Updated `solve_with_provider(...)` to use sticky proxy credentials for provider task creation and return the sticky proxy URL with the token.
- Updated `FlowClient._get_api_captcha_token(...)` to set fingerprint `proxy_url` to `solution.sticky_proxy_url`, ensuring submission reuses the same pinned exit IP.

### Validation

- `python -m compileall src/services/captcha_api_service.py src/services/flow_client.py`
