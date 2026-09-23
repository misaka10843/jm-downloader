"""作者目录构建 + 作者更新检查。

历史实现调用的是 ``client.search_album()``，而 jmcomic 根本没有这个方法，
异常又被 ``except Exception`` 吞掉，所以「检查作者更新」一直是静默失效的。
这里改用真实的 ``client.search_author()``。
"""

from typing import Any, Callable, Dict, List, Optional

from .authors import build_resolver
from .search import search_albums


def build_author_catalog(db) -> List[Dict[str, Any]]:
    """从数据库构建作者目录：规范作者名 + 别名 + 其作品（含封面 / 页数）。

    返回项形如::

        {'canonical': 'DOGYEAR', 'display_name': 'DOGYEAR',
         'aliases': ['DOGYEAR', '九条だんぼ'], 'watched': False,
         'works': [{'album_id': '42337', 'title': ..., 'cover_url': ..., 'page_count': 30}]}
    """
    resolver = build_resolver(db.get_all_author_fields())
    books = db.iter_books()

    # 规范名是按命中次数现算的，新本子入库后可能改名；监听表里存的是旧名，
    # 所以这里要把两边的名字都过一遍 resolver 再比，否则页面上的勾会自己掉。
    watched_canon = {resolver.resolve_entry(w['canonical']) or w['canonical']
                     for w in db.get_watched_authors()}

    by_canonical: Dict[str, List[Dict[str, Any]]] = {}
    for book in books:
        for name in resolver.resolve_field(book.get('author')):
            by_canonical.setdefault(name, []).append(book)

    catalog: List[Dict[str, Any]] = []
    for canonical, aliases in resolver.components().items():
        works = []
        for b in by_canonical.get(canonical, []):
            works.append({
                'album_id': str(b['id']),
                'title': b.get('title'),
                'cover_url': b.get('cover_url'),
                'page_count': b.get('page_count'),
                'tags': b.get('tags'),
                'authors': b.get('author'),
            })
        # 有封面的排前面，方便页面上一眼分辨
        works.sort(key=lambda w: (w.get('cover_url') is None, -(w.get('page_count') or 0)))
        catalog.append({
            'canonical': canonical,
            'display_name': canonical,
            'aliases': aliases,
            'watched': canonical in watched_canon,
            'works': works,
        })

    catalog.sort(key=lambda a: (-len(a['works']), a['canonical']))
    return catalog


def sync_watched_authors(db, resolver=None) -> List[Dict[str, Any]]:
    """把监听表里的名字对齐到当前规范名，返回对齐后的监听列表。

    规范名会随新本子入库而漂移，对齐之后 ``watch`` 才不会插出重复行、
    ``check-update`` 才会用现在的名字去搜、``mark_author_checked`` 也才更新得到那一行。
    """
    if resolver is None:
        resolver = build_resolver(db.get_all_author_fields())
    for w in db.get_watched_authors():
        current = resolver.resolve_entry(w['canonical'])
        if current and current != w['canonical']:
            db.rename_watched_author(w['canonical'], current)
    return db.get_watched_authors()


def check_author_updates(client, db, author_names: List[str], limit: int = 10,
                         on_result: Optional[Callable[[str, Dict[str, Any]], None]] = None
                         ) -> List[Dict[str, Any]]:
    """对每个作者搜索前 ``limit`` 条结果，找出数据库里还没有的作品。

    返回列表，每项::

        {'author': 'DOGYEAR', 'items': [...], 'new': [...], 'error': None}
    """
    known_ids = db.get_book_ids()
    reports: List[Dict[str, Any]] = []

    for name in author_names:
        entry: Dict[str, Any] = {'author': name, 'items': [], 'new': [], 'error': None}
        try:
            items = search_albums(client, name, limit=limit, kind='author', db=db)
            entry['items'] = items
            entry['new'] = [it for it in items if str(it['album_id']) not in known_ids]
            if entry['new']:
                db.mark_author_checked(name, str(entry['new'][0]['album_id']))
            else:
                db.mark_author_checked(name)
        except Exception as e:
            entry['error'] = f'{type(e).__name__}: {e}'
        reports.append(entry)
        if on_result:
            on_result(name, entry)

    return reports
