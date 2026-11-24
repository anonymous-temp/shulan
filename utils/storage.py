# -*- coding: utf-8 -*-
"""文件存储工具。

提供基于文件系统的简单数据持久化功能。
"""
import json
from pathlib import Path
from typing import Optional


class FileStorage:
    """基于文件系统的存储工具。"""

    def __init__(self, base_dir: str = 'data'):
        """初始化存储工具。

        Args:
            base_dir: 数据存储的根目录
        """
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(exist_ok=True)

    def saveHealthReport(self, user_id: str, report_data: dict) -> bool:
        """保存用户的体检报告数据。

        Args:
            user_id: 用户ID
            report_data: 体检报告AI解读结果

        Returns:
            保存是否成功
        """
        try:
            user_dir = self.base_dir / user_id
            user_dir.mkdir(parents=True, exist_ok=True)

            file_path = user_dir / 'health_report.json'
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(report_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as error:
            print(f'保存体检报告失败: {error}')
            return False

    def loadHealthReport(self, user_id: str) -> Optional[dict]:
        """加载用户的体检报告数据。

        Args:
            user_id: 用户ID

        Returns:
            体检报告数据，不存在返回 None
        """
        try:
            file_path = self.base_dir / user_id / 'health_report.json'
            if not file_path.exists():
                return None

            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as error:
            print(f'加载体检报告失败: {error}')
            return None

    def saveSession(self, user_id: str, session_id: str, data: dict) -> bool:
        """保存会话数据。

        Args:
            user_id: 用户ID
            session_id: 会话ID
            data: 会话数据

        Returns:
            保存是否成功
        """
        try:
            session_dir = self.base_dir / user_id / 'sessions'
            session_dir.mkdir(parents=True, exist_ok=True)

            file_path = session_dir / f'{session_id}.json'
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as error:
            print(f'保存会话数据失败: {error}')
            return False

    def loadSession(self, user_id: str, session_id: str) -> Optional[dict]:
        """加载会话数据。

        Args:
            user_id: 用户ID
            session_id: 会话ID

        Returns:
            会话数据，不存在返回 None
        """
        try:
            file_path = self.base_dir / user_id / 'sessions' / f'{session_id}.json'
            if not file_path.exists():
                return None

            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as error:
            print(f'加载会话数据失败: {error}')
            return None

    def deleteSession(self, user_id: str, session_id: str) -> bool:
        """删除会话数据。

        Args:
            user_id: 用户ID
            session_id: 会话ID

        Returns:
            删除是否成功
        """
        try:
            file_path = self.base_dir / user_id / 'sessions' / f'{session_id}.json'
            if file_path.exists():
                file_path.unlink()
            return True
        except Exception as error:
            print(f'删除会话数据失败: {error}')
            return False

    def listUserSessions(self, user_id: str) -> list:
        """列出用户的所有会话ID。

        Args:
            user_id: 用户ID

        Returns:
            会话ID列表
        """
        try:
            session_dir = self.base_dir / user_id / 'sessions'
            if not session_dir.exists():
                return []

            return [f.stem for f in session_dir.glob('*.json')]
        except Exception as error:
            print(f'列出会话失败: {error}')
            return []
