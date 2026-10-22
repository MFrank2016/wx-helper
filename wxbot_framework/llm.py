"""通过配置接入 OpenAI 兼容的大模型接口，不记录密钥或聊天正文。"""
from __future__ import annotations

import json
import logging
import math
from http.client import HTTPException
from ipaddress import ip_address
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LLMError(RuntimeError):
    pass


class LLMFormatError(LLMError):
    """模型响应本身不合规（空结果、结果不是 JSON 对象、格式不正确），属于可重试失败。

    只用于把这一类与连接、超时、HTTP 错误区分开，便于调用方只重试这一类的单次模型调用。
    """
    pass


class LLMQuotaError(LLMError):
    """账户额度或余额不足，短时间内重试没有意义。"""
    pass


class LLMClient:
    def __init__(self, options):
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        base_url = options.get("base_url", "https://api.deepseek.com")
        if not isinstance(base_url, str):
            raise ValueError("大模型 base_url 必须是接口地址字符串")
        self.base_url = base_url.rstrip("/")
        try:
            parsed = urlsplit(self.base_url)
            local = parsed.hostname == "localhost"
            if not local and parsed.hostname:
                try:
                    local = ip_address(parsed.hostname).is_loopback
                except ValueError:
                    pass
            valid = (parsed.scheme == "https" or (parsed.scheme == "http" and local))
            valid = valid and parsed.hostname and parsed.port != 0
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        except ValueError:
            valid = False
        if not valid or any(ch.isspace() or ord(ch) < 32 for ch in base_url):
            raise ValueError("大模型 base_url 应为不含认证信息的 HTTPS 地址；本机回环地址可使用 HTTP")
        self.endpoint = (self.base_url if self.base_url.endswith("/chat/completions")
                         else self.base_url + "/chat/completions")
        self._deepseek_official = (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                                  and parsed.port in (None, 443))
        self.model = options.get("model", "deepseek-flash")
        self._api_key = options.get("api_key", "")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("大模型 model 不能为空")
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise ValueError("请在大模型配置文件中填写 api_key")
        self.timeout = options.get("timeout_seconds", 20)
        self.max_tokens = options.get("max_tokens", 512)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 120:
            raise ValueError("timeout_seconds 应为 1–120 秒")
        if type(self.max_tokens) is not int or not 128 <= self.max_tokens <= 8192:
            raise ValueError("max_tokens 应为 128–8192 的整数")
        self.json_mode = options.get("json_mode", True)
        if type(self.json_mode) is not bool:
            raise ValueError("json_mode 应为布尔值")
        self.token_limit_parameter = options.get("token_limit_parameter", "max_tokens")
        if self.token_limit_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("token_limit_parameter 应为 max_tokens 或 max_completion_tokens")
        extra = options.get("extra_body", {})
        if not isinstance(extra, dict) or not all(isinstance(key, str) for key in extra):
            raise ValueError("extra_body 应为 JSON 对象")
        reserved = {"model", "messages", "stream", "response_format", "max_tokens", "max_completion_tokens",
                    "api_key", "authorization", "Authorization"}
        if reserved.intersection(extra):
            raise ValueError("extra_body 不能覆盖模型、消息、输出格式、令牌上限或认证字段")
        try:
            encoded = json.dumps(extra, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 16384:
                raise ValueError
            # 隔离调用方后续修改，扩展参数只在显式配置时发送。
            self.extra_body = json.loads(encoded)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("extra_body 必须是有效 JSON，且不能超过 16 KiB") from None

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def complete_json(self, system, user):
        json_instruction = "\n\n请只返回一个有效的 JSON 对象，不要使用 Markdown 代码围栏或附加说明。"
        if not self.json_mode:
            system += json_instruction
        payload = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}],
                   self.token_limit_parameter: self.max_tokens,
                   "stream": False, **self.extra_body}
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 保留官方 DeepSeek 旧版的非思考行为；兼容网关不携带专属参数。
        # 用户显式指定的思考设置始终优先，旧的 reasoner 模型也不强制关闭。
        if (self._deepseek_official and self.model in ("deepseek-flash", "deepseek-v4-pro", "deepseek-chat")
                and not {"thinking", "reasoning_effort"}.intersection(self.extra_body)):
            payload["thinking"] = {"type": "disabled"}
        try:
            for attempt in range(2):
                request = Request(self.endpoint,
                                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  headers={"Authorization": "Bearer " + self._api_key,
                                           "Content-Type": "application/json",
                                           # 固定 User-Agent，避免 urllib 默认标识被网关拦截。
                                           "User-Agent": "wx-helper/1.0"}, method="POST")
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise LLMError("大模型响应过大")
                result = json.loads(raw)
                choice = result["choices"][0]
                finish = choice.get("finish_reason")
                message = choice["message"]
                if finish == "length":
                    raise LLMError("大模型响应被截断，请增加 max_tokens")
                if finish == "content_filter" or message.get("refusal"):
                    raise LLMError("大模型接口未提供可用正文，请检查服务商的内容限制")
                if finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
                    raise LLMFormatError("大模型返回了工具调用，未给出内容")
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    reasoning = message.get("reasoning_content")
                    if attempt == 0 and finish in (None, "stop"):
                        if self._deepseek_official and self.json_mode:
                            # 官方 JSON 模式偶发空正文：退回提示词 JSON，仍严格解析。
                            payload.pop("response_format", None)
                            payload["messages"][0]["content"] += json_instruction
                        logging.getLogger("LLM").warning("大模型返回空正文，重试一次")
                        continue
                    suffix = "（只收到思考内容，未取得最终答案）" if isinstance(reasoning, str) and reasoning.strip() else ""
                    raise LLMFormatError("大模型返回了空结果" + suffix)
                return self._extract_json(content, strict=not self.json_mode)
        except HTTPError as error:
            # 只取网关返回的错误说明，不回显请求头或密钥。
            retry_after = None
            raw_retry = error.headers.get("Retry-After") if error.headers else None
            if raw_retry and raw_retry.strip().isdigit():
                retry_after = int(raw_retry.strip())
            detail = ""
            try:
                payload = json.loads(error.read().decode("utf-8", "replace"))
                detail = str(payload.get("error", {}).get("message", "")).strip()[:200]
            except (ValueError, AttributeError, TypeError, OSError):
                detail = ""
            if error.code == 402:
                raise LLMQuotaError(f"大模型账户余额不足：{detail or '请充值后再试'}") from None
            if error.code == 429 and retry_after and retry_after > 300:
                hours = retry_after / 3600
                raise LLMQuotaError(
                    f"大模型免费额度已用尽，约 {hours:.0f} 小时后恢复；请更换模型或开通额度"
                ) from None
            if error.code == 429:
                suffix = f"，建议 {retry_after} 秒后重试" if retry_after else ""
                raise LLMError(f"大模型接口 HTTP 429（限流{suffix}）；{detail or '请稍后再试'}") from None
            raise LLMError(f"大模型接口 HTTP {error.code}；{detail or '请核对密钥、余额和模型配置'}") from None
        except (HTTPException, URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMFormatError("大模型响应格式不正确，请重试") from None

    @staticmethod
    def _extract_json(content, *, strict=False):
        """从正文中取出第一个完整的 JSON 对象。

        正常情况正文就是 JSON；若模型加了说明文字或 ```json 代码块，则按括号配对截取，
        避免把前后说明当成结果。思考内容在 reasoning_content 里，不参与截取。
        strict 用于 json_mode=False 的提示词模式：只接受纯 JSON，不做前后文截取。
        """
        text = content.strip()
        if strict:
            # 提示词模式：只接受纯 JSON 或单个 ```json 代码块，前后不能有说明文字。
            candidate = text
            if text.startswith("```") and text.endswith("```"):
                opening, separator, body = text.partition("\n")
                if separator and opening.strip().lower() in ("```", "```json"):
                    candidate = body[:-3].strip()
            try:
                data = json.loads(candidate)
            except ValueError:
                raise LLMFormatError("大模型响应格式不正确，请重试") from None
            if not isinstance(data, dict):
                raise LLMFormatError("大模型结果应为 JSON 对象")
            return data
        try:
            data = json.loads(text)
        except ValueError:
            pass
        else:
            if not isinstance(data, dict):
                raise LLMFormatError("大模型结果应为 JSON 对象")
            return data
        start = text.find("{")
        if start < 0:
            raise LLMFormatError("大模型响应格式不正确，请重试")
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        data = json.loads(candidate)
                    except ValueError:
                        raise LLMFormatError("大模型响应格式不正确，请重试") from None
                    if not isinstance(data, dict):
                        raise LLMFormatError("大模型结果应为 JSON 对象")
                    tail = text[index + 1:]
                    if "{" in tail or "[" in tail:
                        raise LLMFormatError("大模型返回了多个 JSON 对象")
                    return data
        raise LLMFormatError("大模型响应格式不正确，请重试")
