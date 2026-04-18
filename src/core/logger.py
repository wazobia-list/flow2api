"""Debug logger module for detailed API request/response logging"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlsplit, urlunsplit
from .config import config

class DebugLogger:
    """Debug logger for API requests and responses"""

    def __init__(self):
        self.log_file = Path("logs.txt")
        self._setup_logger()

    def _setup_logger(self):
        """Setup file logger"""
        # Create logger
        self.logger = logging.getLogger("debug_logger")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        # Remove existing handlers
        if self.logger.handlers:
            for handler in list(self.logger.handlers):
                try:
                    handler.close()
                finally:
                    self.logger.removeHandler(handler)

        # Create formatter
        formatter = logging.Formatter(
            '%(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # Create file handler
        file_handler = logging.FileHandler(
            self.log_file,
            mode='a',
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)

        # Add handler
        self.logger.addHandler(file_handler)

    def _ensure_log_file_ready(self):
        """Ensure logger handler points to an existing logs.txt file."""
        if not getattr(self, "logger", None):
            self._setup_logger()
            return

        if self.log_file.exists():
            return

        self._setup_logger()

    def _mask_token(self, token: str) -> str:
        """Mask token for logging (show first 6 and last 6 characters)"""
        if not config.debug_mask_token or len(token) <= 12:
            return token
        return f"{token[:6]}...{token[-6:]}"

    @staticmethod
    def _redact_proxy(proxy_url: Optional[str]) -> Optional[str]:
        if not proxy_url:
            return proxy_url
        try:
            parsed = urlsplit(proxy_url)
            netloc = parsed.netloc or ""
            if "@" not in netloc:
                return proxy_url
            host_part = netloc.rsplit("@", 1)[1]
            return urlunsplit((parsed.scheme, f"<redacted>@{host_part}", parsed.path, parsed.query, parsed.fragment))
        except Exception:
            return proxy_url

    @staticmethod
    def _redact_recap_tokens(data: Any) -> Any:
        if isinstance(data, dict):
            redacted = {}
            for key, value in data.items():
                if key == "token" and isinstance(value, str):
                    redacted[key] = f"<redacted token len={len(value)}>"
                else:
                    redacted[key] = DebugLogger._redact_recap_tokens(value)
            return redacted
        if isinstance(data, list):
            return [DebugLogger._redact_recap_tokens(item) for item in data]
        return data

    def _format_timestamp(self) -> str:
        """Format current timestamp"""
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    def _write_separator(self, char: str = "=", length: int = 100):
        """Write separator line"""
        self.logger.info(char * length)

    def _truncate_large_fields(self, data: Any, max_length: int = 200) -> Any:
        """对大字段进行截断处理，特别是 base64 编码的图片数据
        
        Args:
            data: 要处理的数据
            max_length: 字符串字段的最大长度
        
        Returns:
            截断后的数据副本
        """
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                # 对特定的大字段进行截断
                if key in ("encodedImage", "base64", "imageData", "data") and isinstance(value, str) and len(value) > max_length:
                    result[key] = f"{value[:100]}... (truncated, total {len(value)} chars)"
                else:
                    result[key] = self._truncate_large_fields(value, max_length)
            return result
        elif isinstance(data, list):
            return [self._truncate_large_fields(item, max_length) for item in data]
        elif isinstance(data, str) and len(data) > 10000:
            # 对超长字符串进行截断（可能是未知的 base64 字段）
            return f"{data[:100]}... (truncated, total {len(data)} chars)"
        return data

    def log_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[Any] = None,
        files: Optional[Dict] = None,
        proxy: Optional[str] = None
    ):
        """Log API request details to log.txt"""

        if not config.debug_enabled or not config.debug_log_requests:
            return

        try:
            self._ensure_log_file_ready()
            self._write_separator()
            self.logger.info(f"🔵 [REQUEST] {self._format_timestamp()}")
            self._write_separator("-")

            # Basic info
            self.logger.info(f"Method: {method}")
            self.logger.info(f"URL: {url}")

            # Headers
            self.logger.info("\n📋 Headers:")
            masked_headers = dict(headers)
            if "Authorization" in masked_headers or "authorization" in masked_headers:
                auth_key = "Authorization" if "Authorization" in masked_headers else "authorization"
                auth_value = masked_headers[auth_key]
                if auth_value.startswith("Bearer "):
                    masked_headers[auth_key] = "Bearer <redacted>"

            # Mask Cookie header (ST token)
            if "Cookie" in masked_headers:
                cookie_value = masked_headers["Cookie"]
                if "__Secure-next-auth.session-token=" in cookie_value:
                    parts = cookie_value.split("=", 1)
                    if len(parts) == 2:
                        st_token = parts[1].split(";")[0]
                        masked_headers["Cookie"] = f"__Secure-next-auth.session-token={self._mask_token(st_token)}"

            for key, value in masked_headers.items():
                self.logger.info(f"  {key}: {value}")

            # Body
            if body is not None:
                self.logger.info("\n📦 Request Body:")
                if isinstance(body, (dict, list)):
                    body_str = json.dumps(self._redact_recap_tokens(body), indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                else:
                    self.logger.info(str(body))

            # Files
            if files:
                self.logger.info("\n📎 Files:")
                try:
                    if hasattr(files, 'keys') and callable(getattr(files, 'keys', None)):
                        for key in files.keys():
                            self.logger.info(f"  {key}: <file data>")
                    else:
                        self.logger.info("  <multipart form data>")
                except (AttributeError, TypeError):
                    self.logger.info("  <binary file data>")

            # Proxy
            if proxy:
                self.logger.info(f"\n🌐 Proxy: {self._redact_proxy(proxy)}")

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging request: {e}")

    def log_response(
        self,
        status_code: int,
        headers: Dict[str, str],
        body: Any,
        duration_ms: Optional[float] = None
    ):
        """Log API response details to log.txt"""

        if not config.debug_enabled or not config.debug_log_responses:
            return

        try:
            self._ensure_log_file_ready()
            self._write_separator()
            self.logger.info(f"🟢 [RESPONSE] {self._format_timestamp()}")
            self._write_separator("-")

            # Status
            status_emoji = "✅" if 200 <= status_code < 300 else "❌"
            self.logger.info(f"Status: {status_code} {status_emoji}")

            # Duration
            if duration_ms is not None:
                self.logger.info(f"Duration: {duration_ms:.2f}ms")

            # Headers
            self.logger.info("\n📋 Response Headers:")
            for key, value in headers.items():
                self.logger.info(f"  {key}: {value}")

            # Body
            self.logger.info("\n📦 Response Body:")
            if isinstance(body, (dict, list)):
                # 对大字段进行截断处理
                body_to_log = self._truncate_large_fields(body)
                body_str = json.dumps(body_to_log, indent=2, ensure_ascii=False)
                self.logger.info(body_str)
            elif isinstance(body, str):
                # Try to parse as JSON
                try:
                    parsed = json.loads(body)
                    # 对大字段进行截断处理
                    parsed = self._truncate_large_fields(parsed)
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text (limit length)
                    if len(body) > 2000:
                        self.logger.info(f"{body[:2000]}... (truncated)")
                    else:
                        self.logger.info(body)
            else:
                self.logger.info(str(body))

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging response: {e}")

    def log_error(
        self,
        error_message: str,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None
    ):
        """Log API error details to log.txt"""

        if not config.debug_enabled:
            return

        try:
            self._ensure_log_file_ready()
            self._write_separator()
            self.logger.info(f"🔴 [ERROR] {self._format_timestamp()}")
            self._write_separator("-")

            if status_code:
                self.logger.info(f"Status Code: {status_code}")

            self.logger.info(f"Error Message: {error_message}")

            if response_text:
                self.logger.info("\n📦 Error Response:")
                # Try to parse as JSON
                try:
                    parsed = json.loads(response_text)
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text
                    if len(response_text) > 2000:
                        self.logger.info(f"{response_text[:2000]}... (truncated)")
                    else:
                        self.logger.info(response_text)

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging error: {e}")

    def log_info(self, message: str):
        """Log general info message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self._ensure_log_file_ready()
            self.logger.info(f"ℹ️  [{self._format_timestamp()}] {message}")
        except Exception as e:
            self.logger.error(f"Error logging info: {e}")

    def log_warning(self, message: str):
        """Log warning message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self._ensure_log_file_ready()
            self.logger.warning(f"⚠️  [{self._format_timestamp()}] {message}")
        except Exception as e:
            self.logger.error(f"Error logging warning: {e}")

# Global debug logger instance
debug_logger = DebugLogger()
