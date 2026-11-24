"""统一 AI 调用入口的静态架构测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_AI_SCOPE = (
    PROJECT_ROOT / 'adapters',
    PROJECT_ROOT / 'agents',
    PROJECT_ROOT / 'modules',
    PROJECT_ROOT / 'services',
    PROJECT_ROOT / 'server.py',
)
AION_GATEWAY = PROJECT_ROOT / 'agents' / 'llm' / 'aion_gateway.py'


def _python_files(path: Path):
    if path.is_file():
        yield path
        return
    yield from path.rglob('*.py')


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _direct_model_usages(path: Path) -> list[str]:
    """返回 Service 中绕过 AgentHub 的模型 SDK 使用位置。"""
    usages: list[str] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = node.names
            if module == 'openai' or (module and module.startswith('openai.')):
                usages.append(f'{node.lineno}: import {module}')
            for alias in names:
                imported_name = alias.name
                if isinstance(node, ast.Import):
                    if (
                            imported_name == 'openai'
                            or imported_name.startswith('openai.')
                            or (
                                imported_name.startswith('aion.')
                                and 'chat' in imported_name.split('.')
                            )
                    ):
                        usages.append(
                            f'{node.lineno}: import {imported_name}',
                        )
                elif (
                        module
                        and (module == 'aion' or module.startswith('aion.'))
                        and (
                            imported_name in {'chat', 'create_chat_client'}
                            or 'chat' in module.split('.')
                        )
                ):
                    usages.append(
                        f'{node.lineno}: from {module} import {imported_name}',
                    )
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id == 'create_chat_client':
                usages.append(f'{node.lineno}: create_chat_client(...)')
            elif isinstance(function, ast.Attribute):
                if function.attr in {
                        'create_chat_client',
                        'complete_async',
                        'stream_async',
                }:
                    usages.append(f'{node.lineno}: .{function.attr}(...)')
                elif (
                        function.attr == 'Client'
                        and isinstance(function.value, ast.Name)
                        and function.value.id == 'chat'
                ):
                    usages.append(f'{node.lineno}: chat.Client(...)')
    return usages


def _aion_client_factory_usages(path: Path) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Name) and node.id == 'create_chat_client':
            lines.append(node.lineno)
        elif (
                isinstance(node, ast.Attribute)
                and node.attr == 'create_chat_client'
        ):
            lines.append(node.lineno)
    return lines


class UnifiedAIEntryArchitectureTest(unittest.TestCase):
    """防止生产 AI 调用绕过 AgentHub 与 AionLLMGateway。"""

    def test_production_ai_scope_does_not_call_model_sdk_directly(self) -> None:
        unexpected: list[str] = []
        for scope in PRODUCTION_AI_SCOPE:
            for path in _python_files(scope):
                if path == AION_GATEWAY:
                    continue
                relative_path = path.relative_to(PROJECT_ROOT)
                unexpected.extend(
                    f'{relative_path}:{usage}'
                    for usage in _direct_model_usages(path)
                )

        self.assertEqual(
            unexpected,
            [],
            msg=(
                'adapters/、agents/、modules/、services/ 和 server.py 中的'
                '模型调用必须通过 AgentHub 与 AionLLMGateway'
            ),
        )

    def test_aion_chat_client_factory_is_owned_by_gateway(self) -> None:
        unexpected: list[str] = []
        gateway_usage_found = False
        for scope in PRODUCTION_AI_SCOPE:
            for path in _python_files(scope):
                lines = _aion_client_factory_usages(path)
                if not lines:
                    continue
                if path == AION_GATEWAY:
                    gateway_usage_found = True
                    continue
                relative_path = path.relative_to(PROJECT_ROOT)
                unexpected.extend(
                    f'{relative_path}:{line}' for line in lines
                )

        self.assertTrue(
            gateway_usage_found,
            'AionLLMGateway 应当是 AION Chat Client 的唯一创建位置',
        )
        self.assertEqual(
            unexpected,
            [],
            msg='发现绕过 AionLLMGateway 创建 Chat Client 的生产代码',
        )


if __name__ == '__main__':
    unittest.main()
