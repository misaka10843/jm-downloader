import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, List, Set, Any


class JmDB:
    def __init__(self, path: Path):
        self.path = Path(path).with_suffix('.sqlite')
        self._init_db()

    def _init_db(self):
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

        except sqlite3.DatabaseError:
            self.conn.close()
            new_name = self.path.with_suffix('.sqlite.bak')
            print(
                f"[ERROR] Database file {self.path} is invalid or corrupt. Renaming to {new_name} and creating a fresh DB.")
            if new_name.exists():
                new_name.unlink()
            self.path.rename(new_name)
            # Retry init
            self._init_db()

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
                return json.loads(val)
            except:
                pass
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
        self.cursor.execute("UPDATE books SET download_status = 1 WHERE id = ?", (str(aid),))
        self.conn.commit()

    def save_book(self, album_resp):
        """
        保存本子信息
        """
        aid = str(getattr(album_resp, 'album_id', getattr(album_resp, 'id', None)))
        title = getattr(album_resp, 'title', '')

        # Handle author
        authors_raw = getattr(album_resp, 'author', None) or getattr(album_resp, 'authors', [])
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

        self.cursor.execute('''
            INSERT OR REPLACE INTO books (id, title, author, tags, description, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
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
