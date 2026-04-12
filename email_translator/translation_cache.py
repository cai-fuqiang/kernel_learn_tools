"""翻译缓存 — 基于 SQLite 的本地翻译缓存，避免重复翻译"""
import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TranslationCache:
    """基于 SQLite 的翻译缓存"""

    def __init__(self, cache_dir: str = None):
        """
        Args:
            cache_dir: 缓存目录，默认为项目 data/.cache/
        """
        if cache_dir is None:
            cache_dir = str(Path(__file__).parent.parent / "data" / ".cache")
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(cache_dir) / "translation_cache.db")
        self._init_db()
        self._stats = {"hits": 0, "misses": 0}

    def _init_db(self):
        """初始化数据库表"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    backend TEXT NOT NULL,
                    translated TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    access_count INTEGER DEFAULT 1
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_backend ON cache(backend)")

    @staticmethod
    def _make_key(backend: str, text: str) -> str:
        """生成缓存 key: sha256(backend + text)"""
        content = f"{backend}:{text}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get(self, backend: str, text: str) -> Optional[str]:
        """查询缓存，返回翻译结果或 None"""
        key = self._make_key(backend, text)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT translated FROM cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE cache SET access_count = access_count + 1 WHERE cache_key = ?",
                    (key,),
                )
                self._stats["hits"] += 1
                return row[0]
        self._stats["misses"] += 1
        return None

    def put(self, backend: str, text: str, translated: str):
        """存入缓存"""
        key = self._make_key(backend, text)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (cache_key, backend, translated, created_at) VALUES (?, ?, ?, ?)",
                (key, backend, translated, time.time()),
            )

    def stats(self) -> dict:
        """返回命中统计"""
        total = self._stats["hits"] + self._stats["misses"]
        rate = self._stats["hits"] / total * 100 if total else 0
        return {**self._stats, "total": total, "hit_rate": f"{rate:.1f}%"}

    def size(self) -> int:
        """返回缓存条目数"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM cache").fetchone()
            return row[0] if row else 0

    def clear(self, backend: str = None):
        """清空缓存，可选只清某个后端"""
        with sqlite3.connect(self.db_path) as conn:
            if backend:
                conn.execute("DELETE FROM cache WHERE backend = ?", (backend,))
            else:
                conn.execute("DELETE FROM cache")