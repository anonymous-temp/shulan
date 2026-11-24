"""Shared follow-up CLI options and progress reporting; no service construction."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time


@contextlib.contextmanager
def track_inflight(gateway, task_type: str):
    """记录正在等待模型返回的任务，供心跳打印；出错时打印是哪个任务失败。"""
    inflight = gateway.__dict__.setdefault("inflight", {})
    key = (task_type, time.monotonic())
    inflight[key] = key[1]
    print(f"   → [{task_type}] 已发送请求，等待模型返回…")
    try:
        yield
    except Exception as error:
        print(f"   ✖ [{task_type}] 调用失败（{time.monotonic() - key[1]:.0f}s）: {type(error).__name__}: {error}")
        raise
    finally:
        gateway.last_elapsed = time.monotonic() - inflight.pop(key, key[1])



async def heartbeat(gateway, interval: float = 30.0) -> None:
    """每隔 interval 秒打印一次还在等待的模型调用及已等待时长。"""
    while True:
        await asyncio.sleep(interval)
        inflight = getattr(gateway, "inflight", {})
        if inflight:
            now = time.monotonic()
            waiting = "，".join(f"{task}({now - started:.0f}s)" for (task, started) in sorted(inflight, key=lambda k: k[1]))
            print(f"   ⏳ 仍在等待模型返回 {len(inflight)} 个: {waiting}")



def parse_args(argv=None, *, online=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用评估报告 / 肠道与炎症报告 JSON 驱动后续报告的生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("evaluate_json", help="评估报告回调正文的 JSON 文件路径")
    parser.add_argument("--mode", choices=("fake", "dead", "llm", "api"), default="fake",
                        help="模型形态：fake=返回空串(默认,不花token) / dead=全部抛异常 / "
                             "llm=经 aion 真实调用 / api=走 OpenAI 兼容 HTTP 接口(不需要 aion)")
    parser.add_argument("--api-base", default=None,
                        help="--mode api 的接口根地址，默认取 LLM_API_BASE，"
                             "再默认 https://api.deepseek.com/v1")
    parser.add_argument("--api-key", default=None,
                        help="--mode api 的 Key，默认取 SHULAN_LLM_API_KEY")
    parser.add_argument("--model", default=None,
                        help="--mode api 的模型名，默认取 SHULAN_LLM_DEFAULT_MODEL")
    parser.add_argument("--no-proxy", action="store_true",
                        help="--mode api 时忽略 ALL_PROXY/HTTPS_PROXY 等环境变量，直连接口")
    parser.add_argument("--catalog", choices=("mock", "real", "empty"), default="mock",
                        help="产品目录：mock=本地假数据(默认) / real=真实接口 / empty=模拟目录挂掉")
    parser.add_argument("--deliver", choices=("file", "http"), default="file",
                        help="回调：file=只写本地文件(默认) / http=同时真实 POST")
    parser.add_argument("--out", default=None,
                        help="输出目录，默认 api_test/out；gutBarrier 输入默认 api_test/out/gutBarrier")
    parser.add_argument("--user-id", default="test-user-0001")
    parser.add_argument("--session-id", default="test-session-0001")
    parser.add_argument("--concurrency", type=int, default=0,
                        help="覆盖 reports.llm_semaphore 的并发上限，0 表示不改")
    parser.add_argument("--print-json", nargs="?", const="all", default=None,
                        choices=("all", "instruction", "opinion", "explain", "explain_gutBarrier"),
                        help="把回调报文的完整 JSON 打印到终端："
                             "不带值=三份全打印，也可只打印某一份，如 --print-json instruction")
    parser.add_argument("--print-path", default=None,
                        help="只打印 JSON 里的某一段，点号分隔，数组用下标，"
                             "如 non_drug_intervention.supplements 或 evidence.0")
    parser.add_argument("--verbose", action="store_true", help="打印 DEBUG 级日志")
    args = parser.parse_args(argv)
    uses_api = args.mode in ('llm', 'api') or args.catalog == 'real' or args.deliver == 'http'
    if online is not None and uses_api != online:
        command = 'tests.api.scripts.run_followups' if uses_api else 'tests.unit.tools.run_followups'
        parser.error(f'Use python -m {command} with the same arguments')
    return args
