"""QRA 记忆 provider：Mem0 式增量抽取协议 + 契约权限分离 + Dream 式每日整合。

激活方式：config.yaml 里 ``memory.provider: qra_memory``
发现方式：plugins/memory 扫描器扫 ``$HERMES_HOME/plugins/<name>/``
（通用插件加载器有镜像启发式，会自动跳过本目录，不双载）。

协议（融合架构 v1.0 维度一，全部落在 provider 插件面，核心循环零改动）：

1. **只 ADD 不覆盖**：``on_memory_write`` 只镜像 action=="add"
   （replace/remove 忽略——supermemory 插件同款先例）。
2. **去重对照**：写入前精确去重 + trigram 候选窗口内
   SequenceMatcher≥0.85 判近似重复；对照集 = 本会话已抽（≤20）
   + 库内候选（≤8）。命中即返回 duplicate_of 而不是重复入库。
3. **linked_memory_ids 叙事链**：新记忆自动链接最相关的旧记忆，
   ``qra_memory_detail`` 顺链读出演化史（持仓观点演化正是这个形状）。
4. **expiration_date 过期隐藏**：auto 记忆可带过期天数，到期后
   检索/召回自动隐藏；Dream 周期物理清理过期超 7 天的行。
5. **权限分离**：kind="contract" 是用户文件导入的契约
   （研报铁律/方法论），只读——其他工具拒绝改/删/过期；
   唯一刷新途径是源文件修改后重新 ``qra_contract_import``。
   kind="auto" 是机器写的自动记忆，可过期可合并。
6. **Dream 式每日整合**：provider 内建守护线程（cron agent 以
   skip_memory 运行、走 cron 反而碰执行核心，契约明文允许
   initialize 里起线程）——每 24h 语义去重（≥0.9 合并留旧）、
   清理过期、相对日期转绝对（正则，无 LLM）。

存储：``$HERMES_HOME/qra_memory.db``（SQLite + FTS5 trigram 独立表
手动维护——W2 教训：外部内容表本环境不自动同步，独立表可控）。
写操作全部串行锁，读操作新连接只读。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # 被 MemoryProvider 基类校验 isinstance 时此导入必须成功
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover - 独立单测环境兜底
    MemoryProvider = object  # type: ignore[assignment,misc]

_DREAM_INTERVAL = float(os.environ.get("QRA_MEMORY_DREAM_INTERVAL", "86400"))
_MAX_SESSION_ADDS = 20
_DEDUP_RATIO = 0.85
_ANCHOR_DUP_RATIO = 0.5  # 共享事实锚（小数/长数字/英文词）时的放宽门槛
_MERGE_RATIO = 0.9
_LINK_CANDIDATES = 8
_PREFETCH_MAX_CHARS = 1200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'auto',      -- auto | contract
    source TEXT NOT NULL DEFAULT 'tool',    -- tool | mirror | contract:<文件名>
    session_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    expiration_date TEXT,                   -- ISO；到期即隐藏（contract 恒 NULL）
    linked_memory_ids TEXT NOT NULL DEFAULT '[]',
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_recalled TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
    mem_id UNINDEXED,
    content,
    tokenize='trigram'
);
CREATE INDEX IF NOT EXISTS idx_memories_visible
    ON memories(kind, expiration_date);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _visible_clause() -> str:
    """检索/召回的可见性条件：契约恒可见，auto 未过期可见。"""
    return "(kind='contract' OR expiration_date IS NULL OR expiration_date > ?)"


def _fts_probe(text: str, max_chars: int = 64) -> str:
    """把检索词安全地转成 trigram MATCH 短语（json 引号转义防语法错）。"""
    return json.dumps(text[:max_chars], ensure_ascii=False).strip('"')


_ANCHOR_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9]{3,}|[0-9]{2,}\.[0-9]+|[0-9]{6,}"
)


def _shared_anchors(a: str, b: str) -> bool:
    """共享事实锚：小数价格、≥6 位长数字、≥4 字符英文词。

    量化记忆里数字是事实身份——"1341.99"在任何写法里都指向同一笔
    行情。带连字符的日期("2026-08-14")会被切成短 token，不构成锚，
    避免两条同日不同事实被误判重复。
    """
    return bool(_ANCHOR_PATTERN.findall(a) and
                set(_ANCHOR_PATTERN.findall(a)) & set(_ANCHOR_PATTERN.findall(b)))


def _fuzzy_probe(text: str) -> str:
    """去重/链接/合并用多探针 OR 匹配。

    单一前缀探针在改写差异落在中段时（今日/今天）整窗落空。
    多探针：前缀 + 数字/英文 token（数字是改写中最稳定的部分，
    "1341.99" 在任何写法里都在）——任一命中即可进入候选窗口，
    让 SequenceMatcher 有机会真正比较。
    """
    probes: List[str] = [text[:8]]
    probes += re.findall(r"[A-Za-z0-9]{3,}", text)[:2]
    uniq: List[str] = []
    for p in probes:
        q = _fts_probe(p)
        if q and q not in uniq:
            uniq.append(q)
    return " OR ".join(f'"{q}"' for q in uniq)


class QraMemoryStore:
    """qra_memory.db 的全部读写。写路径串行锁，读路径独立连接。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._self_heal_fts(conn)
        finally:
            conn.close()

    # -- 连接管理 ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _self_heal_fts(self, conn: sqlite3.Connection) -> None:
        """FTS 行数与可见记忆行数不一致时整表重建（自愈，防静默空索引）。"""
        n_mem = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE " + _visible_clause(),
            (_now(),),
        ).fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM mem_fts").fetchone()[0]
        if n_fts != n_mem:
            conn.execute("DELETE FROM mem_fts")
            conn.execute(
                "INSERT INTO mem_fts(mem_id, content) "
                "SELECT id, content FROM memories WHERE " + _visible_clause(),
                (_now(),),
            )
            conn.commit()

    # -- 写路径 -----------------------------------------------------------

    def add(
        self,
        content: str,
        *,
        kind: str = "auto",
        source: str = "tool",
        session_id: str = "",
        expiration_days: Optional[int] = None,
        session_recent: Optional[List[str]] = None,
    ) -> dict:
        """写入前三重对照：会话最近 → 精确重复 → trigram 候选近似重复。"""
        content = content.strip()
        if not content:
            return {"added": False, "error": "content 为空"}
        expiration = None
        if kind == "auto" and expiration_days:
            expiration = (
                datetime.now(timezone.utc) + timedelta(days=int(expiration_days))
            ).isoformat()

        with self._lock:
            conn = self._connect()
            try:
                # 对照一：本会话已抽
                for recent in session_recent or []:
                    if recent.strip() == content:
                        return {"added": False, "duplicate_of": "session_recent",
                                "existing": recent[:80],
                                "advice": "本会话已抽过这条，直接复用即可"}
                # 对照二：库内精确
                row = conn.execute(
                    "SELECT id, content FROM memories WHERE content=? AND "
                    + _visible_clause(),
                    (content, _now()),
                ).fetchone()
                if row:
                    return {"added": False, "duplicate_of": row[0],
                            "existing": row[1][:80],
                            "advice": "已有近似记忆，用 qra_memory_search 查看；"
                                      "有真正的新信息再写"}
                # 对照三：trigram 候选窗口内模糊
                dup, candidates = self._fuzzy_duplicate(conn, content)
                if dup:
                    return {"added": False, "duplicate_of": dup[0],
                            "existing": dup[1][:80], "checked": candidates,
                            "advice": "近似重复（≥0.85），建议更新已有记忆的叙事"
                                      "链而不是新建"}
                # 通过 → 写入 + 链接
                link = self._top_link_candidate(conn, content)
                cur = conn.execute(
                    "INSERT INTO memories(content, kind, source, session_id, "
                    "created_at, expiration_date, linked_memory_ids) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (content, kind, source, session_id, _now(), expiration,
                     json.dumps([link] if link else [], ensure_ascii=False)),
                )
                new_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO mem_fts(mem_id, content) VALUES (?,?)",
                    (new_id, content),
                )
                conn.commit()
                return {"added": True, "id": new_id, "linked_to": [link] if link else [],
                        "dedup_checked": candidates}
            finally:
                conn.close()

    def _like_candidates(self, conn, content: str, limit: int) -> list:
        """二字前缀 LIKE 宽窗。

        trigram 是连续三字符索引——"茅台今日"与"茅台价格"共享二字前缀
        但零 trigram 重叠，trigram 索引搭不上桥。二字 LIKE 把窗口放宽
        到同主题候选，由 SequenceMatcher 比率门槛把关防误判。
        """
        return conn.execute(
            "SELECT id, content FROM memories WHERE content LIKE ? AND "
            + _visible_clause() + " ORDER BY id DESC LIMIT ?",
            (f"%{content[:2]}%", _now(), limit),
        ).fetchall()

    def _fuzzy_duplicate(self, conn, content: str) -> tuple:
        """多探针 trigram 候选窗口（≤8）→ 二字 LIKE 宽窗，SequenceMatcher≥0.85 判重。"""
        if len(content) < 3:
            return None, 0
        rows = []
        try:
            rows = conn.execute(
                "SELECT m.id, m.content FROM mem_fts f "
                "JOIN memories m ON m.id=f.mem_id "
                "WHERE mem_fts MATCH ? AND " + _visible_clause() +
                " ORDER BY bm25(mem_fts) LIMIT ?",
                (_fuzzy_probe(content), _now(), _LINK_CANDIDATES),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        if not rows:  # 探针落空 → 二字 LIKE 宽窗（同主题候选，比率把关）
            rows = self._like_candidates(conn, content, _LINK_CANDIDATES)
        for row_id, existing in rows:
            ratio = SequenceMatcher(None, content, existing).ratio()
            if ratio >= _DEDUP_RATIO or (
                    ratio >= _ANCHOR_DUP_RATIO and _shared_anchors(content, existing)):
                return (row_id, existing), len(rows)
        return None, len(rows)

    def _top_link_candidate(self, conn, content: str) -> Optional[int]:
        if len(content) < 2:
            return None
        try:
            row = conn.execute(
                "SELECT m.id FROM mem_fts f JOIN memories m ON m.id=f.mem_id "
                "WHERE mem_fts MATCH ? AND " + _visible_clause() +
                " ORDER BY bm25(mem_fts) LIMIT 1",
                (_fuzzy_probe(content), _now()),
            ).fetchone()
            if row:
                return row[0]
        except sqlite3.Error:
            pass
        rows = self._like_candidates(conn, content, 1)
        return rows[0][0] if rows else None

    def import_contract(self, path: str) -> dict:
        """从用户文件导入契约记忆（§ 分隔；无 § 则每行一条）。

        同源文件重复导入 = 替换该源的旧契约（用户改文件即刷新，
        这是契约唯一变更途径）。
        """
        p = Path(path).expanduser()
        if not p.is_file():
            return {"imported": 0, "error": f"契约文件不存在：{p}"}
        text = p.read_text(encoding="utf-8", errors="replace")
        if "§" in text:
            entries = [e.strip() for e in re.split(r"§", text)]
        else:
            # 无 § 约定：每条铁律一行（行内内容会保留完整）
            entries = [ln.strip() for ln in text.splitlines() if ln.strip()]
        entries = [e for e in entries if e]
        if not entries:
            return {"imported": 0, "error": "契约文件里没有条目（§ 分隔或空行分段）"}
        source = f"contract:{p.name}"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM mem_fts WHERE mem_id IN "
                             "(SELECT id FROM memories WHERE source=?)", (source,))
                old = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE source=?", (source,)
                ).fetchone()[0]
                conn.execute("DELETE FROM memories WHERE source=?", (source,))
                now = _now()
                for e in entries:
                    cur = conn.execute(
                        "INSERT INTO memories(content, kind, source, created_at) "
                        "VALUES (?,?,?,?)", (e, "contract", source, now))
                    conn.execute("INSERT INTO mem_fts(mem_id, content) VALUES (?,?)",
                                 (cur.lastrowid, e))
                conn.commit()
                return {"imported": len(entries), "replaced": old, "source": p.name,
                        "kind": "contract"}
            finally:
                conn.close()

    def expire(self, mem_id: int) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT kind FROM memories WHERE id=?", (mem_id,)).fetchone()
                if not row:
                    return {"expired": False, "error": f"记忆 {mem_id} 不存在"}
                if row[0] == "contract":
                    return {"expired": False,
                            "error": "契约记忆是用户文件导入的只读内容，不能过期。"
                                     "要修改请改源文件后重新 qra_contract_import"}
                conn.execute(
                    "UPDATE memories SET expiration_date=?, updated_at=? WHERE id=?",
                    (_now(), _now(), mem_id))
                conn.execute("DELETE FROM mem_fts WHERE mem_id=?", (mem_id,))
                conn.commit()
                return {"expired": True, "id": mem_id}
            finally:
                conn.close()

    # -- 读路径 -----------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> List[dict]:
        """trigram（≥3 字符）→ LIKE 兜底；契约优先展示。"""
        conn = self._connect()
        try:
            now = _now()
            if len(query) >= 3:
                try:
                    rows = conn.execute(
                        "SELECT m.id, m.kind, m.content, m.created_at, "
                        "m.hit_count FROM mem_fts f JOIN memories m ON m.id=f.mem_id "
                        "WHERE mem_fts MATCH ? AND " + _visible_clause() +
                        " ORDER BY (m.kind='contract') DESC, bm25(mem_fts) LIMIT ?",
                        (_fts_probe(query), now, limit),
                    ).fetchall()
                    if rows:
                        return self._rows_to_dicts(conn, rows, query)
                except sqlite3.Error:
                    pass
            rows = conn.execute(
                "SELECT id, kind, content, created_at, hit_count FROM memories "
                "WHERE content LIKE ? AND " + _visible_clause() +
                " ORDER BY (kind='contract') DESC LIMIT ?",
                (f"%{query}%", now, limit),
            ).fetchall()
            return self._rows_to_dicts(conn, rows, query)
        finally:
            conn.close()

    def _rows_to_dicts(self, conn, rows, query: str) -> List[dict]:
        out = []
        for row_id, kind, content, created_at, hit_count in rows:
            conn.execute(
                "UPDATE memories SET hit_count=hit_count+1, last_recalled=? "
                "WHERE id=?", (_now(), row_id))
            out.append({"id": row_id, "kind": kind, "content": content,
                        "created_date": (created_at or "")[:10],
                        "hit_count": hit_count + 1})
        conn.commit()
        return out

    def detail(self, mem_id: int, depth: int = 5) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, kind, content, source, session_id, created_at, "
                "updated_at, expiration_date, linked_memory_ids, hit_count, "
                "last_recalled FROM memories WHERE id=?",
                (mem_id,)).fetchone()
            if not row:
                return None
            entry = {
                "id": row[0], "kind": row[1], "content": row[2], "source": row[3],
                "session_id": row[4], "created_at": row[5], "updated_at": row[6],
                "expiration_date": row[7], "linked_memory_ids":
                    json.loads(row[8] or "[]"),
                "hit_count": row[9], "last_recalled": row[10],
            }
            chain = []
            seen = {mem_id}
            frontier = list(entry["linked_memory_ids"])
            while frontier and len(chain) < depth:
                nid = frontier.pop(0)
                if nid in seen:
                    continue
                seen.add(nid)
                nrow = conn.execute(
                    "SELECT id, kind, content, created_at FROM memories WHERE id=?",
                    (nid,)).fetchone()
                if nrow:
                    chain.append({"id": nrow[0], "kind": nrow[1],
                                  "content": nrow[2],
                                  "created_date": (nrow[3] or "")[:10]})
                    nlinks = conn.execute(
                        "SELECT linked_memory_ids FROM memories WHERE id=?",
                        (nid,)).fetchone()
                    if nlinks:
                        frontier.extend(json.loads(nlinks[0] or "[]"))
            entry["chain"] = chain
            return entry
        finally:
            conn.close()

    def prefetch_text(self, query: str, limit: int = 5) -> str:
        """召回：契约优先，格式化到 ≤1200 字符。"""
        hits = self.search(query, limit)
        if not hits:
            return ""
        parts = []
        total = 0
        for h in hits:
            line = (f"【记忆ID:{h['id']} · {h['kind']} · {h['created_date']}】"
                    f"{h['content']}")
            total += len(line)
            if total > _PREFETCH_MAX_CHARS and parts:
                break
            parts.append(line)
        return "\n".join(parts)

    def contract_count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM memories WHERE kind='contract'").fetchone()[0]
        finally:
            conn.close()

    # -- Dream 整合（每日，守护线程调用） --------------------------------

    def dream_maintain(self) -> dict:
        """语义去重（≥0.9 合并留旧）+ 清理过期 + 相对日期转绝对。"""
        with self._lock:
            conn = self._connect()
            try:
                now = _now()
                # 1) 语义去重：只在 trigram 候选窗口内比，避免 O(n²)
                dup_pairs = []
                rows = conn.execute(
                    "SELECT id, content FROM memories WHERE kind='auto' AND "
                    + _visible_clause(), (now,)).fetchall()
                by_id = {r[0]: r[1] for r in rows}
                for mem_id, content in rows:
                    if len(content) < 3:
                        continue
                    try:
                        cands = conn.execute(
                            "SELECT mem_id FROM mem_fts WHERE mem_fts MATCH ? "
                            "LIMIT 8", (_fts_probe(content),)).fetchall()
                    except sqlite3.Error:
                        continue
                    for (cid,) in cands:
                        if cid <= mem_id or cid not in by_id:
                            continue
                        if SequenceMatcher(None, content,
                                           by_id[cid]).ratio() >= _MERGE_RATIO:
                            dup_pairs.append((mem_id, cid))
                merged = 0
                for keep, drop in dup_pairs:
                    links = json.loads(conn.execute(
                        "SELECT linked_memory_ids FROM memories WHERE id=?",
                        (keep,)).fetchone()[0] or "[]")
                    if drop not in links:
                        links.append(drop)
                        conn.execute(
                            "UPDATE memories SET linked_memory_ids=?, updated_at=? "
                            "WHERE id=?", (json.dumps(links, ensure_ascii=False),
                                          now, keep))
                    conn.execute(
                        "UPDATE memories SET expiration_date=?, updated_at=? "
                        "WHERE id=?", (now, now, drop))
                    conn.execute("DELETE FROM mem_fts WHERE mem_id=?", (drop,))
                    merged += 1
                # 2) 物理清理：过期超 7 天且已隐藏的 auto 行
                cutoff = (datetime.now(timezone.utc)
                          - timedelta(days=7)).isoformat()
                cur = conn.execute(
                    "DELETE FROM memories WHERE kind='auto' AND "
                    "expiration_date IS NOT NULL AND expiration_date < ?",
                    (cutoff,))
                purged = cur.rowcount
                # 3) 相对日期转绝对（正则，无 LLM；契约不动）
                normalized = self._normalize_relative_dates(conn, now)
                conn.commit()
                return {"merged": merged, "purged": purged,
                        "normalized": normalized}
            finally:
                conn.close()

    _REL_DATE_PATTERNS = [
        (re.compile(r"昨天"), lambda m: (datetime.now(timezone.utc)
                                         - timedelta(days=1)).date().isoformat()),
        (re.compile(r"今天"), lambda m: datetime.now(timezone.utc).date().isoformat()),
        (re.compile(r"(\d+)\s*天前"),
         lambda m: (datetime.now(timezone.utc)
                    - timedelta(days=int(m.group(1)))).date().isoformat()),
        (re.compile(r"(\d+)\s*周前"),
         lambda m: (datetime.now(timezone.utc)
                    - timedelta(weeks=int(m.group(1)))).date().isoformat()),
        (re.compile(r"上月"),
         lambda m: f"{(datetime.now(timezone.utc) - timedelta(days=30)).year}年"
                   f"{(datetime.now(timezone.utc) - timedelta(days=30)).month}月"),
        (re.compile(r"去年"),
         lambda m: f"{datetime.now(timezone.utc).year - 1}年"),
    ]

    def _normalize_relative_dates(self, conn, now: str) -> int:
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE kind='auto' AND "
            + _visible_clause(), (now,)).fetchall()
        changed = 0
        for mem_id, content in rows:
            new_text = content
            for pattern, repl in self._REL_DATE_PATTERNS:
                new_text = pattern.sub(repl, new_text)
            if new_text != content:
                conn.execute(
                    "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                    (new_text, now, mem_id))
                conn.execute(
                    "DELETE FROM mem_fts WHERE mem_id=?", (mem_id,))
                conn.execute(
                    "INSERT INTO mem_fts(mem_id, content) VALUES (?,?)",
                    (mem_id, new_text))
                changed += 1
        return changed


class QraMemoryProvider(MemoryProvider):
    """QRA 记忆 provider——契约见模块 docstring。"""

    @property
    def name(self) -> str:
        return "qra_memory"

    def is_available(self) -> bool:
        return True  # 纯本地 SQLite，无凭据无网络依赖

    def initialize(self, session_id: str, **kwargs) -> None:
        self.session_id = session_id
        hermes_home = kwargs.get("hermes_home")
        env_db = os.environ.get("QRA_MEMORY_DB", "").strip()
        if env_db:
            db_path = Path(env_db).expanduser()
        elif hermes_home:
            db_path = Path(hermes_home) / "qra_memory.db"
        else:
            db_path = Path.home() / ".hermes" / "qra_memory.db"
        self.db_path = db_path
        self.store = QraMemoryStore(db_path)
        self._session_recent: List[str] = []
        # cron/flush/subagent 上下文不写（防系统提示污染用户画像，supermemory 同款）
        self._write_enabled = kwargs.get("agent_context") not in (
            "cron", "flush", "subagent")
        self._stop_event = threading.Event()
        self._dream_thread = threading.Thread(
            target=self._dream_loop, name="qra-memory-dream", daemon=True)
        self._dream_thread.start()

    def _dream_loop(self) -> None:
        while not self._stop_event.wait(_DREAM_INTERVAL):
            try:
                self.store.dream_maintain()
            except Exception:
                # 守护线程绝不可抛出（会被上层日志吞掉，静默失败最危险）
                import logging
                logging.getLogger(__name__).exception("qra_memory dream 整合失败")

    def shutdown(self) -> None:
        self._stop_event.set()

    def backup_paths(self) -> List[str]:
        return [str(getattr(self, "db_path", ""))]

    # -- 召回 -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        n = self.store.contract_count()
        if not n:
            return ""
        return (f"[QRA记忆] 契约记忆已加载 {n} 条（用户文件导入，只读）。"
                "需要引用研报铁律/方法论时用 qra_memory_search 检索。")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query.strip():
            return ""
        return self.store.prefetch_text(query)

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages=None) -> None:
        # 刻意 no-op：语义抽取需要 LLM（mem0 是服务端 infer），provider 无
        # API 访问。抽取协议改在写路径上执行（工具写 + 镜像写都过 add 三重对照）。
        return None

    # -- 写镜像 -----------------------------------------------------------

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata=None, **kw) -> None:
        if not self._write_enabled:
            return
        if action != "add":  # 只 ADD 不覆盖：replace/remove 镜像忽略
            return
        session_id = ""
        if isinstance(metadata, dict):
            session_id = str(metadata.get("session_id") or "")
        self.store.add(content, kind="auto", source="mirror",
                       session_id=session_id,
                       session_recent=self._session_recent)

    # -- 工具 -------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "qra_memory_add",
                "description": ("新增一条自动记忆（只增不改）。写入前自动去重："
                                "与本会话已记、库内精确、近似（≥85%）对照，"
                                "重复会返回已有记忆而不是重复入库。可设过期天数。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string",
                                    "description": "记忆内容（一条一个事实）"},
                        "expiration_days": {"type": "integer",
                                            "description": "可选：N 天后自动过期隐藏"},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "qra_memory_search",
                "description": ("检索知识库记忆（契约+自动记忆，中英文通吃）。"
                                "契约记忆优先返回。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索词"},
                        "limit": {"type": "integer",
                                  "description": "返回条数，默认 5，最大 10"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "qra_memory_detail",
                "description": ("查看一条记忆的完整信息，并顺叙事链"
                                "（linked_memory_ids）读出相关旧记忆——"
                                "用于看观点的演化历史。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "记忆 ID"},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "qra_memory_expire",
                "description": ("手动过期一条自动记忆（检索/召回立即隐藏）。"
                                "契约记忆不可过期（只读）。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "记忆 ID"},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "qra_contract_import",
                "description": ("把用户维护的契约文件导入为只读契约记忆"
                                "（§ 分隔或每行一条）。同源文件重复导入 = "
                                "按新文件内容整体替换（用户改文件即刷新）。"
                                "契约记忆只能这样变更。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "契约文件路径（如 .hermes/contracts/研报铁律.md）"},
                    },
                    "required": ["path"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any],
                         **kwargs) -> str:
        try:
            if tool_name == "qra_memory_add":
                result = self.store.add(
                    str(args.get("content", "") or ""),
                    session_id=self.session_id,
                    expiration_days=_as_int_or_none(args.get("expiration_days")),
                    session_recent=self._session_recent)
                if result.get("added"):
                    self._session_recent.append(str(args.get("content", "")).strip())
                    if len(self._session_recent) > _MAX_SESSION_ADDS:
                        result["advice"] = ("本会话已记 ≥20 条，建议先 qra_memory_search "
                                            "复核再合并（只 ADD 的代价是条目膨胀）")
                return json.dumps(result, ensure_ascii=False)
            if tool_name == "qra_memory_search":
                limit = max(1, min(_as_int_or_none(args.get("limit")) or 5, 10))
                hits = self.store.search(str(args.get("query", "") or ""), limit)
                return json.dumps({"query": args.get("query", ""),
                                   "hits": len(hits), "results": hits},
                                  ensure_ascii=False)
            if tool_name == "qra_memory_detail":
                entry = self.store.detail(_as_int_or_none(args.get("id")))
                if entry is None:
                    return json.dumps({"error": f"记忆 {args.get('id')} 不存在"},
                                      ensure_ascii=False)
                return json.dumps(entry, ensure_ascii=False)
            if tool_name == "qra_memory_expire":
                return json.dumps(
                    self.store.expire(_as_int_or_none(args.get("id")) or -1),
                    ensure_ascii=False)
            if tool_name == "qra_contract_import":
                return json.dumps(
                    self.store.import_contract(str(args.get("path", "") or "")),
                    ensure_ascii=False)
            return json.dumps({"error": f"未知工具：{tool_name}"},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"qra_memory 处理失败：{e}"},
                              ensure_ascii=False)


def _as_int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def register(ctx) -> None:
    ctx.register_memory_provider(QraMemoryProvider())
