import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, List, Set, Any


_CORRUPTION_MARKERS = (
    'file is not a database',
    'database disk image is malformed',
    'file is encrypted or is not a database',
)


def _looks_corrupt(e: BaseException) -> bool:
    """只有明确表示「这不是 / 已损坏的 sqlite 库」才返回 True。

    ``database is locked`` / ``unable to open database file`` 也是 ``DatabaseError``，
    但那是并发占用或权限问题（临时状态）。按损坏处理会把用户的库改名搬走 = 丢数据。
    """
    msg = str(e).lower()
    return any(m in msg for m in _CORRUPTION_MARKERS)


class JmDB:
    def __init__(self, path: Path):
        self.path = Path(path).with_suffix('.sqlite')
        self._init_db()

    def _init_db(self):
        # 先建父目录：sqlite3.connect 抛的 OperationalError 是 DatabaseError 的子类，
        # 会被下面的「数据库损坏」兜底误判，而那时 self.conn 还没赋值。
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f'无法创建数据库目录 {self.path.parent}: {e}') from e

        # 路径是目录时也会抛 OperationalError，不能因此把用户的目录改名搬走
        if self.path.is_dir():
            raise RuntimeError(f'数据库路径 {self.path} 是一个目录，请检查配置里的 save_db')

        try:
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.cursor = self.conn.cursor()

            # KV
            self.cursor.execute('''
                                CREATE TABLE IF NOT EXISTS kv_store
                                (
                                    key
                                    TEXT
                                    PRIMARY
                                    KEY,
                                    value
                                    TEXT
                                )
                                ''')

            # 本子缓存
            self.cursor.execute('''
                                CREATE TABLE IF NOT EXISTS books
                                (
                                    id
                                    TEXT
                                    PRIMARY
                                    KEY,
                                    title
                                    TEXT,
                                    author
                                    TEXT,
                                    tags
                                    TEXT,
                                    description
                                    TEXT,
                                    updated_at
                                    REAL,
                                    download_status
                                    INTEGER
                                    DEFAULT
                                    0
                                )
                                ''')

            try:
                self.cursor.execute("SELECT download_status FROM books LIMIT 1")
            except sqlite3.OperationalError:
                self.cursor.execute("ALTER TABLE books ADD COLUMN download_status INTEGER DEFAULT 0")

            # 封面 / 总页数（用于 HTML 选择页展示）
            self._ensure_column('books', 'cover_url', 'TEXT')
            self._ensure_column('books', 'page_count', 'INTEGER')
            # 上次尝试补全封面/页数的时间。有些本子（已下架）永远拿不到页数，
            # 没有这个标记就会每次 `ui` 都重新请求一遍。
            self._ensure_column('books', 'extras_checked_at', 'REAL')

            # 监听的作者（canonical 为归一化后的规范作者名）
            self.cursor.execute('''
                                CREATE TABLE IF NOT EXISTS watched_authors
                                (
                                    canonical
                                    TEXT
                                    PRIMARY
                                    KEY,
                                    display_name
                                    TEXT,
                                    aliases
                                    TEXT,
                                    added_at
                                    REAL,
                                    last_checked
                                    REAL,
                                    last_new_id
                                    TEXT
                                )
                                ''')

            # 搜索结果缓存（搜索列表不含页数，需额外请求详情；缓存可避免重复请求）
            self.cursor.execute('''
                                CREATE TABLE IF NOT EXISTS search_cache
                                (
                                    album_id
                                    TEXT
                                    PRIMARY
                                    KEY,
                                    title
                                    TEXT,
                                    cover_url
                                    TEXT,
                                    page_count
                                    INTEGER,
                                    authors
                                    TEXT,
                                    tags
                                    TEXT,
                                    keyword
                                    TEXT,
                                    updated_at
                                    REAL
                                )
                                ''')
            # 缓存命中时也要能还原出详情页链接与收藏状态
            self._ensure_column('search_cache', 'url', 'TEXT')
            self._ensure_column('search_cache', 'is_favorite', 'INTEGER')

            # 本子目录名占用表：目录名不含 album_id，两个本子标题清洗后同名会互相覆盖
            self.cursor.execute('''
                                CREATE TABLE IF NOT EXISTS album_dirs
                                (
                                    dir_name
                                    TEXT
                                    PRIMARY
                                    KEY,
                                    album_id
                                    TEXT
                                    NOT
                                    NULL
                                )
                                ''')

            # cbz状态存储
            self.cursor.execute('''
                                CREATE TABLE IF NOT EXISTS packed
                                (
                                    album_id
                                    TEXT,
                                    photo_id
                                    TEXT,
                                    packed_at
                                    REAL,
                                    PRIMARY
                                    KEY
                                (
                                    album_id,
                                    photo_id
                                )
                                    )
                                ''')
            self.conn.commit()

        except sqlite3.DatabaseError as e:
            conn = getattr(self, 'conn', None)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if not _looks_corrupt(e) or not self.path.exists():
                # 并发占用 / 权限问题也会抛 DatabaseError，但那是临时的。
                # 把它当损坏处理会把用户的库改名搬走 —— 等于静默丢数据。
                raise RuntimeError(f'无法打开数据库 {self.path}: {e}') from e

            new_name = self.path.with_suffix('.sqlite.bak')
            print(
                f"[ERROR] Database file {self.path} is invalid or corrupt. Renaming to {new_name} and creating a fresh DB.")
            if new_name.exists():
                new_name.unlink()
            self.path.rename(new_name)
            # Retry init
            self._init_db()

    def _ensure_column(self, table: str, column: str, decl: str):
        """轻量迁移：列不存在时补上（sqlite 没有 IF NOT EXISTS ADD COLUMN）"""
        try:
            self.cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            self.cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self):
        self.conn.close()

    # KV
    def get_kv(self, key: str, default=None):
        self.cursor.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = self.cursor.fetchone()
        return row['value'] if row else default

    def set_kv(self, key: str, value: str):
        self.cursor.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # 收藏
    def get_fav_latest_id(self) -> Optional[str]:
        return self.get_kv("fav_latest_id")

    def set_fav_latest_id(self, aid: str):
        self.set_kv("fav_latest_id", str(aid))

    def get_fav_list(self) -> List[str]:
        val = self.get_kv("fav_list")
        if val:
            try:
                data = json.loads(val)
            except (TypeError, ValueError):
                return []
            # 必须是 list：kv 里若是「合法 JSON 但不是数组」（手改过 / 旧格式），
            # 直接返回会让调用方 cached_list[:3] 抛 TypeError / KeyError 崩掉整个 download。
            if isinstance(data, list):
                return [str(x) for x in data if x is not None and str(x).strip()]
        return []

    def set_fav_list(self, aids: List[str]):
        self.set_kv("fav_list", json.dumps(aids))

    # 本子
    def get_book(self, aid: str) -> Optional[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM books WHERE id = ?", (str(aid),))
        row = self.cursor.fetchone()
        if row:
            return dict(row)
        return None

    def is_album_completed(self, aid: str) -> bool:
        row = self.get_book(aid)
        if row and row.get('download_status') == 1:
            return True
        return False

    def mark_album_completed(self, aid: str):
        # 用 upsert：books 里没有该本子时裸 UPDATE 会静默不生效
        self.cursor.execute('''
            INSERT INTO books (id, download_status) VALUES (?, 1)
            ON CONFLICT(id) DO UPDATE SET download_status = 1
        ''', (str(aid),))
        self.conn.commit()

    def save_book(self, album_resp):
        aid = str(getattr(album_resp, 'album_id', getattr(album_resp, 'id', None)))
        title = getattr(album_resp, 'title', '')

        # 必须优先取 authors（完整列表）。JmAlbumDetail.author 是个 property，
        # 只返回 authors[0]，用它会把合作作者全部丢掉 —— 而作者监听正是靠 author 字段。
        authors_raw = getattr(album_resp, 'authors', None) or getattr(album_resp, 'author', None) or []
        authors_list = []
        if isinstance(authors_raw, list):
            authors_list = authors_raw
        elif isinstance(authors_raw, str):
            authors_list = [authors_raw]

        valid_authors = []
        for a in authors_list:
            if a and a.strip() and a.strip().lower() not in ('unknown', 'none', '未知', 'default_author'):
                valid_authors.append(a.strip())

        author_str = ','.join(valid_authors)

        tags = getattr(album_resp, 'tags', [])
        tags_str = ','.join(tags) if isinstance(tags, list) else str(tags)

        desc = getattr(album_resp, 'description', '') or getattr(album_resp, 'summary', '')

        # 不能用 INSERT OR REPLACE：它会先删整行，把 download_status 重置回 0 触发重复下载
        self.cursor.execute('''
            INSERT INTO books (id, title, author, tags, description, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                author = excluded.author,
                tags = excluded.tags,
                description = excluded.description,
                updated_at = excluded.updated_at
        ''', (aid, title, author_str, tags_str, str(desc), time.time()))
        self.conn.commit()

    def get_all_authors(self) -> Set[str]:
        self.cursor.execute("SELECT author FROM books")
        authors = set()
        for row in self.cursor.fetchall():
            a_str = row['author']
            if a_str:
                parts = a_str.split(',')
                for p in parts:
                    p = p.strip()
                    if p and p.lower() != 'unknown' and p.lower() != 'none':
                        authors.add(p)
        return authors

    # Packed Status
    def mark_packed(self, album_id: str, photo_id: str):
        self.cursor.execute('''
            INSERT OR REPLACE INTO packed (album_id, photo_id, packed_at)
            VALUES (?, ?, ?)
        ''', (str(album_id), str(photo_id), time.time()))
        self.conn.commit()

    def is_packed(self, album_id: str, photo_id: str) -> bool:
        self.cursor.execute("SELECT 1 FROM packed WHERE album_id = ? AND photo_id = ?", (str(album_id), str(photo_id)))
        return self.cursor.fetchone() is not None

    # ------------------------------------------------------------------
    # 本子 / 封面 / 页数
    # ------------------------------------------------------------------
    def iter_books(self) -> List[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM books")
        return [dict(r) for r in self.cursor.fetchall()]

    def get_all_author_fields(self) -> List[str]:
        """返回 books 表里所有非空的 author 原始字段（逗号分隔的作者串）。"""
        self.cursor.execute("SELECT author FROM books WHERE author IS NOT NULL AND author != ''")
        return [r['author'] for r in self.cursor.fetchall()]

    def set_book_extras(self, aid: str, cover_url: Optional[str] = None,
                        page_count: Optional[int] = None):
        """给已存在的本子补上封面/页数（仅更新已有行，不会凭空建行）。"""
        sets, params = [], []
        if cover_url:
            sets.append('cover_url = ?')
            params.append(cover_url)
        if page_count:
            sets.append('page_count = ?')
            params.append(int(page_count))
        if not sets:
            return
        params.append(str(aid))
        self.cursor.execute(f"UPDATE books SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def mark_extras_checked(self, aid: str):
        """记下「已尝试补全封面/页数」，避免对拿不到数据的本子反复请求。"""
        self.cursor.execute(
            "UPDATE books SET extras_checked_at = ? WHERE id = ?", (time.time(), str(aid)))
        self.conn.commit()

    def books_missing_extras(self, force: bool = False) -> List[Dict[str, Any]]:
        """缺封面或页数、且尚未尝试过补全的本子（``force=True`` 忽略「已尝试」标记）。"""
        if force:
            self.cursor.execute(
                "SELECT * FROM books WHERE cover_url IS NULL OR page_count IS NULL")
        else:
            self.cursor.execute(
                "SELECT * FROM books WHERE extras_checked_at IS NULL"
                " AND (cover_url IS NULL OR page_count IS NULL)")
        return [dict(r) for r in self.cursor.fetchall()]

    def get_book_ids(self) -> Set[str]:
        self.cursor.execute("SELECT id FROM books")
        return {r['id'] for r in self.cursor.fetchall()}

    # ------------------------------------------------------------------
    # 本子目录名（避免不同本子写进同一个目录）
    # ------------------------------------------------------------------
    def get_album_dir_name(self, album_id: str) -> Optional[str]:
        """返回本子已占用的目录名；没有记录则返回 None。"""
        self.cursor.execute("SELECT dir_name FROM album_dirs WHERE album_id = ?", (str(album_id),))
        row = self.cursor.fetchone()
        return row['dir_name'] if row else None

    def resolve_album_dir_name(self, album_id: str, base_name: str) -> str:
        """返回该本子应使用的目录名，同名冲突时追加 ``[album_id]``。

        结果会写库，所以同一本子每次运行都用同一个目录名；历史目录因为库里没有记录、
        名字又空闲，会被原样沿用。
        """
        aid = str(album_id)
        existing = self.get_album_dir_name(aid)
        if existing:
            return existing

        candidate = base_name
        n = 1
        while True:
            self.cursor.execute("SELECT album_id FROM album_dirs WHERE dir_name = ?", (candidate,))
            row = self.cursor.fetchone()
            if row is None or row['album_id'] == aid:
                break
            # 别的本子标题字面就是 "作品 [123]" 时仍可能再撞，继续加后缀
            candidate = f"{base_name} [{aid}]" if n == 1 else f"{base_name} [{aid}]({n})"
            n += 1

        self.cursor.execute(
            "INSERT OR REPLACE INTO album_dirs (dir_name, album_id) VALUES (?, ?)", (candidate, aid))
        self.conn.commit()
        return candidate

    def forget_album_dir_name(self, album_id: str) -> bool:
        """释放本子占用的目录名（删除本子记录时用）。"""
        self.cursor.execute("DELETE FROM album_dirs WHERE album_id = ?", (str(album_id),))
        self.conn.commit()
        return self.cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 搜索结果缓存
    # ------------------------------------------------------------------
    def upsert_search_item(self, item: Dict[str, Any]):
        self.cursor.execute('''
            INSERT INTO search_cache (album_id, title, cover_url, page_count, authors, tags, keyword, url, is_favorite, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_id) DO UPDATE SET
                title = excluded.title,
                cover_url = excluded.cover_url,
                page_count = COALESCE(excluded.page_count, search_cache.page_count),
                authors = excluded.authors,
                tags = excluded.tags,
                keyword = excluded.keyword,
                url = COALESCE(excluded.url, search_cache.url),
                is_favorite = COALESCE(excluded.is_favorite, search_cache.is_favorite),
                updated_at = excluded.updated_at
        ''', (
            str(item.get('album_id')), item.get('title'), item.get('cover_url'),
            item.get('page_count'), item.get('authors'), item.get('tags'),
            item.get('keyword'), item.get('url'),
            None if item.get('is_favorite') is None else int(bool(item.get('is_favorite'))),
            time.time(),
        ))
        self.conn.commit()

    def upsert_search_items(self, items: List[Dict[str, Any]]):
        for it in items:
            self.upsert_search_item(it)

    def get_cached_search_item(self, album_id: str) -> Optional[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM search_cache WHERE album_id = ?", (str(album_id),))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def iter_search_cache(self) -> List[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM search_cache ORDER BY updated_at DESC")
        return [dict(r) for r in self.cursor.fetchall()]

    # ------------------------------------------------------------------
    # 监听作者
    # ------------------------------------------------------------------
    def get_watched_authors(self) -> List[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM watched_authors ORDER BY added_at")
        out = []
        for row in self.cursor.fetchall():
            d = dict(row)
            try:
                d['aliases'] = json.loads(d.get('aliases') or '[]')
            except Exception:
                d['aliases'] = []
            out.append(d)
        return out

    def is_watched(self, canonical: str) -> bool:
        self.cursor.execute("SELECT 1 FROM watched_authors WHERE canonical = ?", (str(canonical),))
        return self.cursor.fetchone() is not None

    def get_watched_author(self, canonical: str) -> Optional[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM watched_authors WHERE canonical = ?", (str(canonical),))
        row = self.cursor.fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d['aliases'] = json.loads(d.get('aliases') or '[]')
        except Exception:
            d['aliases'] = []
        return d

    def rename_watched_author(self, old: str, new: str) -> None:
        """把监听行改名到 ``new``（规范名漂移后用）；``new`` 已存在则合并两行。

        不合并的话同一作者会留下两行，``check-update`` 就会把同一个作者搜两遍。
        """
        old, new = str(old), str(new)
        if old == new:
            return
        old_row = self.get_watched_author(old)
        if old_row is None:
            return
        new_row = self.get_watched_author(new)
        if new_row is None:
            self.cursor.execute(
                "UPDATE watched_authors SET canonical = ? WHERE canonical = ?", (new, old))
        else:
            merged = list(dict.fromkeys((new_row.get('aliases') or [])
                                        + (old_row.get('aliases') or [])))
            self.cursor.execute(
                "UPDATE watched_authors SET aliases = ?, display_name = COALESCE(display_name, ?),"
                " last_checked = COALESCE(last_checked, ?), last_new_id = COALESCE(last_new_id, ?)"
                " WHERE canonical = ?",
                (json.dumps(merged, ensure_ascii=False), old_row.get('display_name'),
                 old_row.get('last_checked'), old_row.get('last_new_id'), new))
            self.cursor.execute("DELETE FROM watched_authors WHERE canonical = ?", (old,))
        self.conn.commit()

    def add_watched_author(self, canonical: str, display_name: Optional[str] = None,
                           aliases: Optional[List[str]] = None) -> bool:
        """返回 True 表示新增，False 表示已存在（仅更新别名）。"""
        existed = self.is_watched(canonical)
        self.cursor.execute('''
            INSERT INTO watched_authors (canonical, display_name, aliases, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(canonical) DO UPDATE SET
                display_name = excluded.display_name,
                aliases = excluded.aliases
        ''', (str(canonical), display_name or canonical,
              json.dumps(aliases or [], ensure_ascii=False), time.time()))
        self.conn.commit()
        return not existed

    def remove_watched_author(self, canonical: str) -> bool:
        self.cursor.execute("DELETE FROM watched_authors WHERE canonical = ?", (str(canonical),))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def clear_watched_authors(self):
        self.cursor.execute("DELETE FROM watched_authors")
        self.conn.commit()

    def mark_author_checked(self, canonical: str, last_new_id: Optional[str] = None):
        self.cursor.execute(
            "UPDATE watched_authors SET last_checked = ?, last_new_id = COALESCE(?, last_new_id) WHERE canonical = ?",
            (time.time(), last_new_id, str(canonical)))
        self.conn.commit()
