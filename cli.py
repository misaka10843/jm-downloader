import argparse
import time
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from jm_downloader.authors import build_resolver
from jm_downloader.config import DownloaderConfig, build_config, load_config_from_yaml, resolve_config_path
from jm_downloader.db import JmDB
from jm_downloader.downloader import JmFavDownloader, create_client
from jm_downloader.search import (KINDS, backfill_book_extras, books_missing_extras,
                                  export_csv, export_json, load_selection,
                                  make_selection, search_albums)
from jm_downloader.updates import build_author_catalog, check_author_updates, sync_watched_authors
from jm_downloader.utils import setup_logging
from jm_downloader.webui import render_author_page, render_search_page

console = Console()

DEFAULT_UI_DIR = './jm_ui'
COMMANDS = ['download', 'check-update', 'search', 'ui', 'watch', 'favorite']


def _non_negative_int(value: str) -> int:
    """argparse 的 type=：拒绝负数。

    负数在切片里是「从末尾倒数」，会把「最多取 N 个」静默变成「除了最后 N 个」，
    必须当场报错，而不是让用户以为只取了几条。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'需要整数，实际是 {value!r}')
    if n < 0:
        raise argparse.ArgumentTypeError(f'不能为负数，实际是 {n}')
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='JM 收藏下载器 - modular')
    parser.add_argument('command', nargs='?', choices=COMMANDS, default='download',
                        help='执行命令: download(默认) / check-update / search / ui / watch / favorite')
    parser.add_argument('--config', '-c', help='YAML 配置文件路径', default=None)
    parser.add_argument('--album', '-a', nargs='*', help='指定 album id 列表', default=[])
    parser.add_argument('--username', '-u', help='JM 登录用户名', default=None)
    parser.add_argument('--password', '-p', help='JM 登录密码', default=None)
    parser.add_argument('--no-fav', action='store_true', help='不要下载收藏夹')
    parser.add_argument('--cbz-dir', help='CBZ 输出目录（覆盖配置文件）', default=None)
    parser.add_argument('--originals-dir', help='原图输出目录（覆盖配置文件）', default=None)
    parser.add_argument('--delete-after-pack', action='store_true', help='CBZ 打包成功后删除对应原图')
    parser.add_argument('--keep-originals', action='store_true', help='CBZ 打包后保留原图（覆盖配置文件）')

    # --- 新增：搜索 / 选择页 / 监听 / 收藏 ---
    parser.add_argument('--from-file', default=None,
                        help='HTML 选择页导出的 JSON（selection.json / watchlist.json）')
    parser.add_argument('--keyword', '-k', help='搜索关键词', default=None)
    parser.add_argument('--kind', choices=sorted(KINDS), default='site',
                        help='搜索方式: site(站内,默认) / author / work / tag / actor')
    parser.add_argument('--limit', type=_non_negative_int, default=10,
                        help='每个作者 / 每次搜索最多取多少条（默认 10）')
    parser.add_argument('--out-dir', '-o', default=DEFAULT_UI_DIR,
                        help=f'JSON / CSV / HTML 的导出目录（默认 {DEFAULT_UI_DIR}）')
    parser.add_argument('--no-html', action='store_true', help='只导出 JSON/CSV，不生成 HTML 页面')
    parser.add_argument('--no-covers', action='store_true',
                        help='ui: 不联网补全封面/页数（离线生成页面）')
    parser.add_argument('--cover-limit', type=_non_negative_int, default=None,
                        help='ui: 本次最多补全多少个本子的封面/页数（默认不限，0 表示不补）')
    parser.add_argument('--refresh-covers', action='store_true',
                        help='ui: 强制重新补全（默认只补从没补过的本子）')
    parser.add_argument('--all-authors', action='store_true',
                        help='check-update: 忽略监听列表，检查数据库里的全部作者')
    parser.add_argument('--list', action='store_true', help='watch: 列出当前监听的作者')
    parser.add_argument('--add', nargs='*', default=[], help='watch: 直接添加作者（可多个）')
    parser.add_argument('--remove', nargs='*', default=[], help='watch: 移除作者（可多个）')
    parser.add_argument('--clear', action='store_true', help='watch: 清空监听列表')
    return parser


def load_cfg(args) -> DownloaderConfig:
    config_path = resolve_config_path(args.config)
    if config_path and not args.config:
        console.log(f'[blue]未指定 -c，自动读取 {config_path}[/blue]')
    cfg_data = load_config_from_yaml(config_path)
    cfg = build_config(
        cfg_data,
        username=args.username,
        password=args.password,
        # None 表示「命令行没表态」，此时才去读配置文件里的 download_favorites
        download_favorites=False if args.no_fav else None,
        album_ids=args.album,
    )
    if args.cbz_dir:
        cfg.cbz_dir = Path(args.cbz_dir)
    if args.originals_dir:
        cfg.originals_dir = Path(args.originals_dir)
    if args.keep_originals:
        cfg.delete_after_pack = False
    elif args.delete_after_pack:
        cfg.delete_after_pack = True
    return cfg


def selection_album_ids(path) -> list:
    sel = load_selection(Path(path))
    return [str(a['album_id']) for a in sel['albums']]


# ----------------------------------------------------------------------
# download
# ----------------------------------------------------------------------
def cmd_download(cfg: DownloaderConfig, args):
    cfg.ensure_dirs()
    downloader = JmFavDownloader(cfg)

    album_ids = []
    if args.from_file:
        album_ids.extend(selection_album_ids(args.from_file))
        console.print(f'[blue]从选择文件读取到 {len(album_ids)} 个 album[/blue]')
    if cfg.album_ids:
        album_ids.extend([a for a in cfg.album_ids if a not in album_ids])
    elif not args.from_file and cfg.download_favorites:
        favs = downloader.get_favorites_album_ids()
        album_ids.extend([a for a in favs if a not in album_ids])

    if not album_ids:
        console.print('[yellow]未找到要下载的本子（既没有指定 album 也未获取到收藏）[/yellow]')
        return
    downloader.download_album_list(album_ids)


# ----------------------------------------------------------------------
# check-update
# ----------------------------------------------------------------------
def cmd_check_update(cfg: DownloaderConfig, args):
    db = JmDB(cfg.save_db)
    # 监听表里可能存着漂移前的旧名，先对齐，否则会搜错名字、也更新不到那一行
    watched = sync_watched_authors(db)
    catalog = build_author_catalog(db)
    if not catalog:
        console.print('[yellow]数据库里还没有作者记录，请先下载一些本子积累缓存。[/yellow]')
        return

    if args.all_authors or not watched:
        names = [a['canonical'] for a in catalog]
        if not watched:
            console.print('[yellow]尚未设置监听作者，本次检查全部作者。'
                          '建议先执行 `ui` 生成选择页，再用 `watch --from-file` 保存监听列表。[/yellow]')
    else:
        names = [w['canonical'] for w in watched]

    console.print(f'[blue]检查 {len(names)} 位作者（每人最多前 {args.limit} 条搜索结果）...[/blue]')
    client = create_client(cfg)

    with Progress(SpinnerColumn(), TextColumn('[progress.description]{task.description}'),
                  console=console) as prog:
        task = prog.add_task('准备中...', total=len(names))

        def on_result(name, entry):
            mark = f"[green]+{len(entry['new'])}[/green]" if entry['new'] else '[grey]0[/grey]'
            prog.update(task, advance=1, description=f'{name} 新作品 {mark}')

        reports = check_author_updates(client, db, names, limit=args.limit, on_result=on_result)

    new_items, failed = [], []
    for r in reports:
        if r['error']:
            failed.append((r['author'], r['error']))
        new_items.extend(r['new'])

    if new_items:
        table = Table('作者', 'album_id', '页数', '标题')
        for r in reports:
            for it in r['new']:
                table.add_row(r['author'], str(it['album_id']),
                              str(it.get('page_count') or '-'), (it.get('title') or '')[:60])
        console.print(table)
    else:
        console.print('[green]没有发现新作品[/green]')

    for author, err in failed:
        console.print(f'[red]检查 {author} 失败: {err}[/red]')

    if new_items:
        out_dir = Path(args.out_dir)
        jp = export_json(out_dir / 'updates.json',
                         make_selection('albums', albums=new_items, keyword='作者更新'))
        cp = export_csv(out_dir / 'updates.csv', new_items)
        console.print(f'[green]新作品清单: {jp}[/green]')
        console.print(f'[green]新作品清单: {cp}[/green]')
        if not args.no_html:
            hp = render_search_page(new_items, out_dir / 'updates.html',
                                    keyword='作者更新', kind='author')
            console.print(f'[green]已生成选择页: {hp}[/green]')


# ----------------------------------------------------------------------
# search
# ----------------------------------------------------------------------
def cmd_search(cfg: DownloaderConfig, args):
    if not args.keyword:
        console.print('[red]请用 -k/--keyword 指定搜索关键词[/red]')
        return

    db = JmDB(cfg.save_db)
    client = create_client(cfg)
    console.print(f'[blue]搜索 "{args.keyword}"（{args.kind}），最多 {args.limit} 条...[/blue]')

    items = search_albums(client, args.keyword, limit=args.limit, kind=args.kind, db=db)
    if not items:
        console.print('[yellow]没有搜索结果[/yellow]')
        return

    table = Table('album_id', '页数', '标题', '作者', '已收藏')
    for it in items:
        table.add_row(str(it['album_id']), str(it.get('page_count') or '-'),
                      (it.get('title') or '')[:58], (it.get('authors') or '')[:22],
                      '是' if it.get('is_favorite') else '')
    console.print(table)

    out_dir = Path(args.out_dir)
    jp = export_json(out_dir / 'search_result.json',
                     make_selection('albums', albums=items, keyword=args.keyword))
    cp = export_csv(out_dir / 'search_result.csv', items)
    console.print(f'[green]已导出: {jp}[/green]')
    console.print(f'[green]已导出: {cp}[/green]')
    if not args.no_html:
        hp = render_search_page(items, out_dir / 'search_result.html',
                                keyword=args.keyword, kind=args.kind)
        console.print(f'[green]已生成选择页: {hp}[/green]')
        console.print('[blue]在页面里勾选作品 → 导出 selection.json → '
                      '`download --from-file` 下载，或 `favorite --from-file` 加入收藏[/blue]')


# ----------------------------------------------------------------------
# ui
# ----------------------------------------------------------------------
def cmd_ui(cfg: DownloaderConfig, args):
    db = JmDB(cfg.save_db)
    catalog = build_author_catalog(db)
    if not catalog:
        console.print('[yellow]数据库里还没有作者记录，请先下载一些本子积累缓存。[/yellow]')
        return

    # 页面靠封面来分辨同名/多写法的作者，而存量库里 cover_url 全是空的，
    # 所以生成页面前先按需补一次（可用 --no-covers 跳过）。
    if not args.no_covers:
        pending = books_missing_extras(db, force=args.refresh_covers)
        # 用 is not None 而不是真值判断：--cover-limit 0 的语义是「一本都不补」，
        # 当成假值会把「不补」执行成「全补」—— 方向正好相反。
        if args.cover_limit is not None:
            pending = pending[:args.cover_limit]
        if pending:
            client = create_client(cfg)
            console.print(f'[blue]为 {len(pending)} 部作品补全封面/页数（每部一次详情请求）...[/blue]')
            with Progress(SpinnerColumn(), TextColumn('{task.description}'),
                          console=console, transient=True) as progress:
                task = progress.add_task('补全中...', total=len(pending))

                def on_progress(index, total, aid, error):
                    progress.update(task, completed=index,
                                    description=f'补全封面 {index}/{total}')

                stats = backfill_book_extras(client, db, limit=args.cover_limit,
                                             force=args.refresh_covers,
                                             on_progress=on_progress)
            msg = f'[green]补全 {stats["filled"]} 部[/green]'
            if stats['unavailable']:
                msg += f'，[yellow]{stats["unavailable"]} 部拿不到页数（已下架？不再重试）[/yellow]'
            if stats['failed']:
                msg += f'，[yellow]{stats["failed"]} 部请求失败（下次会重试）[/yellow]'
            console.print(msg)
            catalog = build_author_catalog(db)   # 用补全后的数据重建

    out = render_author_page(catalog, Path(args.out_dir) / 'authors.html',
                            meta={'db': str(cfg.save_db)})
    works = sum(len(a['works']) for a in catalog)
    with_cover = sum(1 for a in catalog for w in a['works'] if w.get('cover_url'))
    console.print(f'[green]已生成作者选择页: {out}[/green]')
    console.print(f'  共 {len(catalog)} 位作者 / {works} 部作品（{with_cover} 部有封面）；'
                  f'同一作者的不同写法已合并，别名显示在作者名下方')
    console.print('[blue]浏览器打开 → 勾选要监听的作者 → 导出 watchlist.json → '
                  '`python cli.py watch --from-file watchlist.json`[/blue]')


# ----------------------------------------------------------------------
# watch
# ----------------------------------------------------------------------
def cmd_watch(cfg: DownloaderConfig, args):
    db = JmDB(cfg.save_db)
    resolver = build_resolver(db.get_all_author_fields())
    # 先对齐旧名，否则同一个作者会因为改名被当成两个而插出重复行
    sync_watched_authors(db, resolver)

    def canon(name: str) -> str:
        """把页面/用户给的名字归一化成规范作者名（未知名字原样保留）。"""
        return resolver.resolve_entry(name) or name

    if args.clear:
        db.clear_watched_authors()
        console.print('[yellow]已清空监听列表[/yellow]')

    added = removed = 0
    if args.from_file:
        sel = load_selection(Path(args.from_file))
        if not sel['authors']:
            console.print('[yellow]选择文件里没有作者条目（是不是选到了作品选择文件？）[/yellow]')
        for a in sel['authors']:
            raw = a.get('canonical') or a.get('display_name')
            if not raw:
                continue
            name = canon(raw)
            if db.add_watched_author(name, a.get('display_name') or name,
                                     a.get('aliases') or [name]):
                added += 1
                console.print(f'  [green]+ {name}[/green]')
            else:
                console.print(f'  [grey]= {name}（已在列表中，已更新别名）[/grey]')

    for raw in args.add:
        name = canon(raw)
        if db.add_watched_author(name, name, [name]):
            added += 1
            console.print(f'  [green]+ {name}[/green]')
    for raw in args.remove:
        if db.remove_watched_author(canon(raw)):
            removed += 1
            console.print(f'  [yellow]- {raw}[/yellow]')

    if added:
        console.print(f'[green]新增监听 {added} 位作者[/green]')
    if removed:
        console.print(f'[yellow]移除监听 {removed} 位作者[/yellow]')

    watched = db.get_watched_authors()
    if not watched:
        console.print('[yellow]当前监听列表为空。可先执行 `ui` 生成选择页，'
                      '或用 `watch --add 作者名` 直接添加。[/yellow]')
        return

    table = Table('作者', '别名', '上次检查')
    for w in watched:
        ts = w.get('last_checked')
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)) if ts else '从未'
        table.add_row(w.get('display_name') or w['canonical'],
                      ', '.join(w.get('aliases') or []) or '-', when)
    console.print(table)
    console.print(f'[blue]共 {len(watched)} 位。执行 `python cli.py check-update` 检查更新。[/blue]')


# ----------------------------------------------------------------------
# favorite
# ----------------------------------------------------------------------
def cmd_favorite(cfg: DownloaderConfig, args):
    ids = []
    if args.from_file:
        ids = selection_album_ids(args.from_file)
        console.print(f'[blue]从选择文件读取到 {len(ids)} 个 album[/blue]')
    ids.extend(str(a) for a in (args.album or []) if str(a) not in ids)

    if not ids:
        console.print('[red]没有要收藏的作品。用 --from-file 指定选择文件，或用 --album 指定 id。[/red]')
        return

    if not (cfg.username and cfg.password):
        console.print('[yellow]未配置账号密码，收藏需要登录。请用 -u/-p 或写进 config.yml。[/yellow]')

    client = create_client(cfg)
    ok = dup = fail = unknown = 0

    for aid in ids:
        # add_favorite_album 底层是 toggle：对已收藏的本子调用会先「取消收藏」。
        # 所以只认明确的 True/False，拿不到明确状态一律跳过。
        try:
            album = client.get_album_detail(aid)
            raw_flag = getattr(album, 'is_favorite', None)
        except Exception as e:
            unknown += 1
            console.print(f'  [yellow]{aid} 无法确认收藏状态，已跳过（避免误取消收藏）: {str(e)[:80]}[/yellow]')
            continue

        if raw_flag is None:
            # 不能用 bool(... or False) 兜底成「未收藏」，赌错会取消掉用户的收藏
            unknown += 1
            console.print(f'  [yellow]{aid} 接口未返回 is_favorite，已跳过（避免误取消收藏）[/yellow]')
            continue

        if bool(raw_flag):
            dup += 1
            console.print(f'  [grey]{aid} 已在收藏夹，跳过[/grey]')
            continue

        try:
            client.add_favorite_album(aid)
            ok += 1
            console.print(f'  [green]{aid} 已加入收藏[/green]')
        except Exception as e:
            fail += 1
            console.print(f'  [red]{aid} 收藏失败: {str(e)[:120]}[/red]')

    console.print(f'[bold]收藏完成: 成功 {ok}，已存在 {dup}，失败 {fail}，状态未知跳过 {unknown}[/bold]')


HANDLERS = {
    'download': cmd_download,
    'check-update': cmd_check_update,
    'search': cmd_search,
    'ui': cmd_ui,
    'watch': cmd_watch,
    'favorite': cmd_favorite,
}

# 这些命令需要联网 / 需要下载目录；ui 与 watch 纯本地，不该顺手创建下载目录
NEEDS_DOWNLOAD_DIRS = ('download', 'check-update', 'search', 'favorite')


def main():
    args = build_parser().parse_args()
    try:
        cfg = load_cfg(args)
    except (FileNotFoundError, IsADirectoryError, ValueError) as e:
        # 配置写错时给一句人话，而不是把 traceback 甩到用户脸上
        console.print(f'[red]配置有误: {e}[/red]')
        raise SystemExit(2)

    setup_logging()

    if args.command in NEEDS_DOWNLOAD_DIRS:
        cfg.ensure_dirs()

    if args.command in ('download', 'check-update'):
        console.log(f'[blue]配置载入：原图 {cfg.originals_root}，CBZ {cfg.cbz_root}，重试 {cfg.retries}，'
                    f'清洗标题 {cfg.extract_title}，打包后删除原图 {cfg.delete_after_pack}[/blue]')

    HANDLERS[args.command](cfg, args)


if __name__ == '__main__':
    main()
