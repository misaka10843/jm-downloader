"""搜索本子，并把结果导出成 JSON / CSV 供 HTML 选择页使用。

jmcomic 没有 ``client.search_album()``（历史上本仓库调用过它，是无效调用）。真实接口是
``search_site`` / ``search_work`` / ``search_author`` / ``search_tag`` / ``search_actor``
（对应 main_tag 0~4），都返回 ``JmSearchPage``。

列表页只有 ``album_id`` 和 ``title``，**不含总页数**，要额外请求一次 ``get_album_detail``；
详情结果会缓存进 ``search_cache`` 表避免重复请求。
"""

import csv
import html as html_lib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jmcomic import JmcomicText

try:
    from jmcomic import MissingAlbumPhotoException as _MissingAlbumPhotoException
except ImportError:  # pragma: no cover
    class _MissingAlbumPhotoException(Exception):
        pass

# 本子已下架 / id 不存在时 get_album_detail 抛这个（永久状态，不必重试）；
# 其它异常一律视为临时失败，要保留重试机会。
_PERMANENT_DETAIL_ERRORS = (_MissingAlbumPhotoException,)

_WS_RE = re.compile(r'[\s\u3000]+')

SITE_URL_TEMPLATE = 'https://18comic.vip/album/{album_id}'

# 关键词 -> 客户端方法名
KINDS = {
    'site': 'search_site',
    'work': 'search_work',
    'author': 'search_author',
    'tag': 'search_tag',
    'actor': 'search_actor',
}


def normalize_title(value: Any) -> str:
    """搜索结果标题里会混入 &nbsp; 之类的 HTML 实体，统一清理掉。"""
    if not value:
        return ''
    text = html_lib.unescape(str(value))
    return _WS_RE.sub(' ', text).strip()


def album_cover_url(album_id: Any, size: str = '_3x4') -> Optional[str]:
    """根据本子 id 生成封面 url（浏览器直接引用即可，CDN 只校验 UA）。"""
    try:
        return JmcomicText.get_album_cover_url(str(album_id), size=size)
    except Exception:
        return None


def album_page_url(album_id: Any) -> str:
    return SITE_URL_TEMPLATE.format(album_id=album_id)


def fetch_album_brief(client, album_id: Any, db=None, force: bool = False,
                      keyword: Optional[str] = None, raise_on_error: bool = False
                      ) -> Dict[str, Any]:
    """获取单个本子的摘要：id / 标题 / 封面 / 总页数 / 作者 / 标签 / 是否已收藏。

    带 sqlite 缓存：已缓存且拿到过 page_count 的条目直接复用，避免重复请求。
    详情失败时默认不抛（一次搜索不该因单个本子整体失败）；需要区分永久/临时失败的
    调用方传 ``raise_on_error=True``。
    """
    aid = str(album_id)

    if db is not None and not force:
        cached = db.get_cached_search_item(aid)
        if cached and cached.get('page_count'):
            # url 是纯派生值（缓存表里可能为空），这里兜底重建；
            # is_favorite 在 sqlite 里存的是 0/1，还原成 bool
            cached['url'] = cached.get('url') or album_page_url(aid)
            cached['is_favorite'] = (None if cached.get('is_favorite') is None
                                     else bool(cached.get('is_favorite')))
            cached['from_cache'] = True
            return cached

    item: Dict[str, Any] = {
        'album_id': aid,
        'title': None,
        'cover_url': album_cover_url(aid),
        'page_count': None,
        'authors': None,
        'tags': None,
        'is_favorite': None,
        'url': album_page_url(aid),
        'keyword': keyword,
    }

    detail_error: Optional[BaseException] = None
    try:
        album = client.get_album_detail(aid)
        item['title'] = normalize_title(getattr(album, 'name', None))
        item['authors'] = ','.join(getattr(album, 'authors', None) or []) or None
        item['tags'] = ','.join(getattr(album, 'tags', None) or []) or None
        item['page_count'] = getattr(album, 'page_count', None)
        item['is_favorite'] = bool(getattr(album, 'is_favorite', False))
    except Exception as e:
        detail_error = e

    # 失败时不要写缓存：写进去的是 title/authors/tags 全空的空壳行，会把上一次
    # 成功缓存的标题 / 作者 / 标签覆盖掉（page_count 有 COALESCE 才幸免）。
    if db is not None and detail_error is None:
        db.upsert_search_item(item)

    if detail_error is not None and raise_on_error:
        raise detail_error

    item['from_cache'] = False
    return item


def search_albums(client, keyword: str, limit: int = 10, kind: str = 'site',
                  db=None, force: bool = False) -> List[Dict[str, Any]]:
    """搜索并返回前 ``limit`` 条结果（默认 10 条）。

    先取搜索列表页，再用列表页标题兜底，最后逐条补详情拿页数。
    """
    method_name = KINDS.get(kind)
    if method_name is None:
        # 不静默退回站内搜索：kind 写错会搜到另一个索引，用户却以为搜的是作者。
        raise ValueError(f'未知的搜索方式: {kind!r}（可用: {", ".join(sorted(KINDS))}）')
    method = getattr(client, method_name, None)
    if method is None:
        raise ValueError(f'当前 jmcomic 客户端不支持搜索方式: {kind} ({method_name})')

    page = method(keyword, page=1)

    ordered_ids: List[str] = []
    fallback_titles: Dict[str, str] = {}
    for aid, title in page.iter_id_title():
        sid = str(aid)
        if sid not in fallback_titles:
            ordered_ids.append(sid)
        fallback_titles[sid] = normalize_title(title)

    results: List[Dict[str, Any]] = []
    for aid in ordered_ids[:max(0, limit)]:
        item = fetch_album_brief(client, aid, db=db, force=force, keyword=keyword)
        if not item.get('title'):
            item['title'] = fallback_titles.get(aid) or aid
        results.append(item)
    return results


def books_missing_extras(db, force: bool = False) -> List[Dict[str, Any]]:
    """books 表里缺封面或页数、且尚未尝试补全的行（用于决定要不要补数据）。"""
    return db.books_missing_extras(force=force)


def backfill_book_extras(client, db, limit: Optional[int] = None, force: bool = False,
                         on_progress=None) -> Dict[str, int]:
    """给库里已有但缺封面/页数的本子补数据。

    作者选择页要靠封面分辨作者，而 cover_url / page_count 只有走过详情请求才有值，
    存量库全是空的。这里按需补一次（结果同时写进 search_cache）。

    三种结果的重试策略不同：``filled`` 拿到了数据；``unavailable`` 是永久状态
    （已下架 / 确实没有页数），标记后不再重试；``failed`` 是临时失败，保留重试机会。
    单个本子出问题不影响其它本子。
    """
    todo = books_missing_extras(db, force=force)
    # limit=0 必须表示「一个都不补」；用真值判断会变成「不限」。
    # 负数要拦住：切片里的负数是「从末尾倒数」，会静默变成「除了最后 N 个」。
    if limit is not None:
        if limit < 0:
            raise ValueError(f'limit 不能为负数: {limit}')
        todo = todo[:limit]

    filled = unavailable = failed = 0
    for index, row in enumerate(todo, 1):
        aid = str(row['id'])
        error = None
        try:
            # 必须让原异常抛出来，否则区分不了永久失败和临时失败
            item = fetch_album_brief(client, aid, db=db, raise_on_error=True)
            db.set_book_extras(aid, cover_url=item.get('cover_url'),
                               page_count=item.get('page_count'))
            if item.get('page_count'):
                filled += 1
            else:
                unavailable += 1
                db.mark_extras_checked(aid)
        except _PERMANENT_DETAIL_ERRORS as e:
            unavailable += 1
            error = e
            db.mark_extras_checked(aid)
        except Exception as e:          # noqa: BLE001 - 单个本子失败要能继续
            # 不能 mark_extras_checked，否则一次网络抖动就让这个本子永远缺封面
            failed += 1
            error = e
        if on_progress:
            on_progress(index, len(todo), aid, error)

    return {'total': len(todo), 'filled': filled,
            'unavailable': unavailable, 'failed': failed}


# ----------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------
CSV_FIELDS = ['album_id', 'title', 'page_count', 'authors', 'tags',
              'cover_url', 'url', 'is_favorite', 'keyword']


def export_json(path: Path, payload: Dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def export_csv(path: Path, items: List[Dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig 让 Excel 打开中文不乱码
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction='ignore')
        writer.writeheader()
        for it in items:
            writer.writerow({k: it.get(k) for k in CSV_FIELDS})
    return path


def make_selection(selection_type: str, *, albums: Optional[List[Dict[str, Any]]] = None,
                   authors: Optional[List[Dict[str, Any]]] = None,
                   keyword: Optional[str] = None) -> Dict[str, Any]:
    """构造 HTML 页面导出的选择文件结构。"""
    payload: Dict[str, Any] = {
        'type': selection_type,          # 'albums' | 'authors'
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'albums': albums or [],
        'authors': authors or [],
    }
    if keyword:
        payload['keyword'] = keyword
    return payload


def load_selection(path: Path) -> Dict[str, Any]:
    """读取并校验 HTML 页面导出的选择文件。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f'选择文件不存在: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise ValueError(f'选择文件不是合法 JSON: {path} ({e})') from e
    if not isinstance(data, dict):
        raise ValueError(f'选择文件格式不对，顶层应为对象: {path}')

    albums = data.get('albums') or []
    authors = data.get('authors') or []
    if not isinstance(albums, list) or not isinstance(authors, list):
        raise ValueError(f'选择文件格式不对：albums / authors 应为数组: {path}')

    data['albums'] = [a for a in albums if isinstance(a, dict) and a.get('album_id')]
    data['authors'] = [a for a in authors if isinstance(a, dict)]
    if not data['albums'] and not data['authors']:
        raise ValueError(f'选择文件里没有任何条目: {path}')
    return data
