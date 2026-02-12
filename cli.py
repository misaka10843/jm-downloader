import argparse
from pathlib import Path

from rich.console import Console
from rich.table import Table

from jm_downloader.config import DownloaderConfig, load_config_from_yaml
from jm_downloader.db import JmDB
from jm_downloader.downloader import JmFavDownloader
from jm_downloader.utils import setup_logging

console = Console()


# Todo: 可能需要优化作者名(因为很多本子的作者名会出现 名称(名称2))
def check_updates(cfg: DownloaderConfig):
    db = JmDB(cfg.save_db)
    authors = db.get_all_authors()

    if not authors:
        console.print("[yellow]数据库中没有作者记录，请先下载一些本子积累缓存。[/yellow]")
        return

    console.print(f"[blue]正在检查 {len(authors)} 位作者的更新...[/blue]")

    downloader = JmFavDownloader(cfg)
    client = downloader.client

    updated_authors = []

    for author in authors:
        try:
            search_gen = client.search_album(author, page=1)
            found_new = False
            latest_id_found = None

            page = search_gen
            first_page = None
            if hasattr(search_gen, 'iter_id_title'):
                first_page = search_gen
            else:
                try:
                    first_page = next(search_gen)
                except StopIteration:
                    pass

            if not first_page:
                continue

            for aid, atitle in first_page.iter_id_title():
                aid = str(aid)
                if not db.get_book(aid):
                    found_new = True
                    latest_id_found = aid
                    break
                else:
                    break

            if found_new:
                updated_authors.append((author, latest_id_found))
                console.print(f"  [green]发现更新: {author} (最新ID: {latest_id_found})[/green]")
        except Exception as e:
            console.print(f"  [red]检查作者 {author} 失败: {e}[/red]")

    if updated_authors:
        console.rule("[bold green]更新汇总[/bold green]")
        table = Table("作者", "最新书籍ID (未入库)")
        for auth, aid in updated_authors:
            table.add_row(auth, aid)
        console.print(table)
    else:
        console.print("[green]所有作者均为最新状态 (或未发现新书)[/green]")


def main():
    parser = argparse.ArgumentParser(description='JM 收藏下载器 - modular')
    parser.add_argument('command', nargs='?', choices=['download', 'check-update'], default='download',
                        help='执行命令: download (默认) 或 check-update')
    parser.add_argument('--config', '-c', help='YAML 配置文件路径', default=None)
    parser.add_argument('--album', '-a', nargs='*', help='指定 album id 列表', default=[])
    parser.add_argument('--username', '-u', help='JM 登录用户名', default=None)
    parser.add_argument('--password', '-p', help='JM 登录密码', default=None)
    parser.add_argument('--no-fav', action='store_true', help='不要下载收藏夹')
    args = parser.parse_args()

    cfg_data = load_config_from_yaml(args.config)
    cfg = DownloaderConfig(
        out_dir=Path(cfg_data.get('out_dir', './downloads')),
        retries=int(cfg_data.get('retries', 3)),
        delete_after_pack=bool(cfg_data.get('delete_after_pack', False)),
        extract_title=bool(cfg_data.get('extract_title', False)),
        jm_option_file=Path(cfg_data['jm_option_file']) if cfg_data.get('jm_option_file') else None,
        username=args.username or cfg_data.get('username'),
        password=args.password or cfg_data.get('password'),
        download_favorites=not args.no_fav and cfg_data.get('download_favorites', True),
        album_ids=args.album or cfg_data.get('album_ids', []),
        save_db=Path(cfg_data.get('save_db', './downloads_db.sqlite'))
    )

    cfg.ensure_dirs()
    setup_logging()

    if args.command == 'check-update':
        check_updates(cfg)
        return

    console.log(f'[blue]配置载入：输出 {cfg.out_dir}，重试 {cfg.retries}，清洗标题 {cfg.extract_title}[/blue]')

    downloader = JmFavDownloader(cfg)
    album_ids = []
    if cfg.album_ids:
        album_ids.extend(cfg.album_ids)
    elif cfg.download_favorites:
        favs = downloader.get_favorites_album_ids()
        album_ids.extend([a for a in favs if a not in album_ids])
    if not album_ids:
        console.print('[yellow]未找到要下载的本子（既没有指定 album 也未获取到收藏）[/yellow]')
        return
    downloader.download_album_list(album_ids)


if __name__ == '__main__':
    main()
