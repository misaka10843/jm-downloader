import argparse
import re
import shutil
from pathlib import Path

from rich.console import Console
from rich.progress import track

from jm_downloader.cbz_packer import CbzPacker
from jm_downloader.config import build_config, load_config_from_yaml, resolve_config_path
from jm_downloader.db import JmDB
from jm_downloader.utils import setup_logging, clean_title_for_filename, legacy_clean_title_for_filename

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='JM Repacker - Repack existing folders with new metadata')
    parser.add_argument('--config', '-c', help='YAML 配置文件路径', default=None)
    parser.add_argument('--username', '-u', help='JM 登录用户名', default=None)
    parser.add_argument('--password', '-p', help='JM 登录密码', default=None)
    parser.add_argument('--cbz-dir', help='CBZ 输出目录（覆盖配置文件）', default=None)
    parser.add_argument('--originals-dir', help='原图目录（覆盖配置文件）', default=None)
    parser.add_argument('--delete-after-pack', action='store_true', help='打包成功后删除对应原图')
    parser.add_argument('--keep-originals', action='store_true', help='打包后保留原图（覆盖配置文件）')
    return parser


def main():
    args = build_parser().parse_args()

    # Load Config to get paths
    try:
        config_path = resolve_config_path(args.config)
        if config_path and not args.config:
            console.log(f"[blue]未指定 -c，自动读取 {config_path}[/blue]")
        cfg_data = load_config_from_yaml(config_path)
    except (FileNotFoundError, IsADirectoryError, ValueError) as e:
        console.log(f"[red]配置有误: {e}[/red]")
        return
    cfg = build_config(
        cfg_data,
        username=args.username,
        password=args.password,
        download_favorites=False,
        album_ids=[],
    )
    if args.cbz_dir:
        cfg.cbz_dir = Path(args.cbz_dir)
    if args.originals_dir:
        cfg.originals_dir = Path(args.originals_dir)
    if args.keep_originals:
        cfg.delete_after_pack = False
    elif args.delete_after_pack:
        cfg.delete_after_pack = True

    setup_logging()
    db = JmDB(cfg.save_db)

    console.log("[blue]读取数据库中书籍信息...[/blue]")
    books = {}
    try:
        db.cursor.execute("SELECT * FROM books")
        for row in db.cursor.fetchall():
            books[row['id']] = dict(row)
    except Exception as e:
        console.log(f"[red]读取数据库失败: {e}[/red]")
        return

    if not books:
        console.log("[yellow]数据库为空，无法进行元数据匹配重打包[/yellow]")
        return

    originals_dir = cfg.originals_root
    if not originals_dir.exists():
        console.log(f"[red]找不到原来的图片目录: {originals_dir}[/red]")
        return

    console.log(f"[blue]原图目录: {originals_dir}[/blue]")
    console.log(f"[blue]CBZ 输出目录: {cfg.cbz_root}[/blue]")
    if cfg.delete_after_pack:
        console.log("[yellow]已开启打包后删除原图 (delete_after_pack)[/yellow]")

    count = 0

    console.log(f"[blue]开始扫描 {len(books)} 本已记录的书籍...[/blue]")

    for aid, book in track(books.items(), description="Repacking..."):
        raw_title = book['title']

        candidates = set()
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=True, max_len=180))
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=False, max_len=180))
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=True, max_len=200))
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=False, max_len=200))
        # 兼容旧版本括号清洗规则生成的历史目录名
        candidates.add(legacy_clean_title_for_filename(raw_title, max_len=180))
        candidates.add(legacy_clean_title_for_filename(raw_title, max_len=200))

        found_path = None

        # 先按库里的记录找：下载时若目录名冲突会被追加 [album_id]，那种名字不在候选里。
        # 候选集合是无序的，所以这里必须优先判断。
        try:
            recorded_dir = db.get_album_dir_name(aid)
        except Exception:
            recorded_dir = None
        if recorded_dir:
            p = originals_dir / recorded_dir
            if p.exists() and p.is_dir():
                found_path = p

        if not found_path:
            for cand in candidates:
                p = originals_dir / cand
                if p.exists() and p.is_dir():
                    found_path = p
                    break

        if not found_path:
            continue

        cbz_base = cfg.cbz_root / found_path.name
        cbz_base.mkdir(parents=True, exist_ok=True)
        authors_str = book['author']
        tags_str = book['tags']
        summary = book['description']
        cbz_series = clean_title_for_filename(raw_title, extract_brackets=cfg.extract_title, max_len=999)

        for chap_dir in list(found_path.iterdir()):
            if not chap_dir.is_dir():
                continue

            chap_name = chap_dir.name

            num = 1.0
            m = re.search(r'第(\d+)话', chap_name)
            if m:
                num = float(m.group(1))

            cbz_file = cbz_base / f"{chap_name}.cbz"

            try:
                CbzPacker.pack_images_to_cbz(
                    images_folder=chap_dir,
                    cbz_path=cbz_file,
                    title=f"{chap_name} - {cbz_series}",
                    series=cbz_series,
                    number=num,
                    authors=authors_str,
                    tags=tags_str,
                    summary=summary,
                    album_id=aid
                )
                db.mark_packed(aid, chap_name)
                if cfg.delete_after_pack:
                    shutil.rmtree(chap_dir, ignore_errors=True)
                    console.print(f"[grey]已删除原图文件夹: {chap_dir}[/grey]")
            except Exception as e:
                console.print(f"[red]打包失败 {found_path.name}/{chap_name}: {e}[/red]")

        # 章节全部删除后，清理本子留下的空目录
        if cfg.delete_after_pack and cfg.delete_empty_album_dir:
            try:
                if found_path.exists() and not any(found_path.iterdir()):
                    found_path.rmdir()
                    console.print(f"[grey]已删除空的原始目录: {found_path}[/grey]")
            except OSError:
                pass

        count += 1

    console.log(f"[green]重打包完成，共处理 {count} 本[/green]")


if __name__ == '__main__':
    main()
