"""Follow-up acceptance with an explicit real model, product catalog or HTTP callback."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.unit._support.harnesses.followup_options import parse_args
from tests.unit._support.harnesses.followup_options import track_inflight

BASE_DIR = Path(__file__).resolve().parents[3]
SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "report" / "followups" / "system.txt"
_DOTENV_CACHE: dict[str, str] | None = None


def _dotenv(name: str, default: str = "") -> str:
    """取配置：系统环境变量优先，其次项目根的 .env。

    自己解析 .env，避免依赖私有包 loongspears.utils.env。
    .env 里的键带 SHULAN_ 前缀，这里按 config/env.py 的约定自动补。
    """
    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        _DOTENV_CACHE = {}
        env_file = BASE_DIR / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                _DOTENV_CACHE[key.strip()] = value.strip().strip("'\"")
    for key in (name, f"SHULAN_{name}"):
        value = os.environ.get(key) or _DOTENV_CACHE.get(key)
        if value:
            return value
    return default



class OpenAICompatModelGateway:
    """直接走 OpenAI 兼容的 HTTP 接口，不依赖私有包 aion。

    DeepSeek、通义、vLLM、one-api 等都兼容 /chat/completions 协议，
    所以没装 aion 的机器也能用真实模型验证生成质量。
    只用 httpx（项目已有依赖）。
    """

    def __init__(self, *, api_base: str, api_key: str, model: str,
                 timeout: float = 600.0, trust_env: bool = True) -> None:
        import httpx  # noqa: PLC0415

        if not api_key:
            raise SystemExit(
                "❌ 缺少 API Key。用 --api-key 传，或在 .env / 环境变量里配 "
                "SHULAN_LLM_API_KEY。"
            )
        if not model:
            raise SystemExit(
                "❌ 缺少模型名。用 --model 传，或在 .env / 环境变量里配 "
                "SHULAN_LLM_DEFAULT_MODEL。"
            )
        try:
            # trust_env=False 时忽略 ALL_PROXY / HTTPS_PROXY 等环境变量，
            # 避免本机挂了 SOCKS 代理（梯子）时 httpx 因缺 socksio 直接崩。
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                trust_env=trust_env,
            )
        except ImportError as error:
            raise SystemExit(
                f"❌ httpx 初始化失败：{error}\n"
                "   本机环境变量里配了 SOCKS 代理，但缺 socksio。二选一：\n"
                '     1) python -m pip install "httpx[socks]"   # 继续走代理\n'
                "     2) 加 --no-proxy 参数                      # 这次调用绕开代理"
            ) from error
        self._url = api_base.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._system_content = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        self.calls: list[str] = []
        print(f"   模型接口: {self._url}  model={model}")

    async def complete_text(self, *, task_type: str, user_prompt: str) -> str:
        self.calls.append(task_type)
        with track_inflight(self, task_type):
            response = await self._client.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": self._system_content},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            response.raise_for_status()
            body = response.json()
        choices = body.get("choices") or [{}]
        content = (choices[0].get("message") or {}).get("content") or ""
        print(f"   ↩ [{task_type}] 模型返回 {len(content)} 字符（{self.last_elapsed:.0f}s）")
        return content

    async def aclose(self) -> None:
        await self._client.aclose()



class AionModelGateway:
    """真实模型调用，走和线上同一个 provider / model / system prompt。

    直接用 aion.create_chat_client，不启动整个 AgentHub，省掉 Mongo/Redis 依赖；
    调用参数（context=False、thinking=False、system_content）与
    agents/llm/aion_gateway.py 保持一致。
    """

    def __init__(self) -> None:
        try:
            import aion  # noqa: PLC0415
            from config.env import (  # noqa: PLC0415
                LLM_API_KEY,
                LLM_DEFAULT_MODEL,
                LLM_PROVIDER,
            )
        except ModuleNotFoundError as error:
            raise SystemExit(
                f"❌ --mode llm 需要私有包（缺 {error.name}）。三条路选一条：\n"
                "   1) 换到已装好依赖的环境：conda env list 看看有没有 shulan 环境\n"
                '   2) 装依赖：pip install "aion[all] @ '
                'git+https://gitee.com/loongspears/aion@4c4a64f46d3f8053d06db28addf10bd65dc4974d"\n'
                "   3) 改用 --mode api：直接走 OpenAI 兼容 HTTP 接口，只需要 httpx"
            ) from error

        self._aion = aion
        self._provider = LLM_PROVIDER
        self._api_key = LLM_API_KEY
        self._model = LLM_DEFAULT_MODEL
        self._system_content = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        self.calls: list[str] = []

    async def complete_text(self, *, task_type: str, user_prompt: str) -> str:
        self.calls.append(task_type)
        kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "context": False,
            "system_content": self._system_content,
        }
        if self._model:
            kwargs["model"] = self._model
        # 与线上一致：每次调用新建无状态 client，避免并发共享上下文。
        client = self._aion.create_chat_client(self._provider, **kwargs)
        with track_inflight(self, task_type):
            result = await client.complete_async(user_prompt, thinking=False)
        content = getattr(result, "content", None) or ""
        print(f"   ↩ [{task_type}] 模型返回 {len(content)} 字符（{self.last_elapsed:.0f}s）")
        return content



def create_model_gateway(args):
    from tests.unit._support.harnesses.run_followups import DeadModelGateway
    from tests.unit._support.harnesses.run_followups import FakeModelGateway

    if args.mode == "api":
        model_gateway = OpenAICompatModelGateway(
            api_base=args.api_base or _dotenv("LLM_API_BASE", "https://api.deepseek.com/v1"),
            api_key=args.api_key or _dotenv("LLM_API_KEY"),
            model=args.model or _dotenv("LLM_DEFAULT_MODEL"),
            trust_env=not args.no_proxy,
        )
    else:
        model_gateway = {
            "fake": FakeModelGateway,
            "dead": DeadModelGateway,
            "llm": AionModelGateway,
        }[args.mode]()
    return model_gateway


def create_gateway(args, out_dir):
    from tests.unit._support.harnesses.run_followups import LocalGateway

    http_gateway = None
    if args.catalog == "real" or args.deliver == "http":
        from adapters.report.followup import FollowupReportHttpGateway  # noqa: PLC0415
        from config.env import REPORT_CALLBACK_URL, REPORT_PRODUCT_URL  # noqa: PLC0415

        http_gateway = FollowupReportHttpGateway(
            callback_url=REPORT_CALLBACK_URL,
            product_catalog_url=REPORT_PRODUCT_URL,
        )
        print(f"   真实回调地址: {REPORT_CALLBACK_URL}")
        print(f"   真实目录地址: {REPORT_PRODUCT_URL}")
    return LocalGateway(catalog=args.catalog, deliver=args.deliver, out_dir=out_dir, http_gateway=http_gateway)


def main():
    args = parse_args(online=True)
    from tests.unit._support.harnesses.run_followups import async_main

    try:
        sys.exit(asyncio.run(async_main(
            args, model_gateway_factory=create_model_gateway, gateway_factory=create_gateway,
        )))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == '__main__':
    main()
