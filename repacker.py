import argparse
from pathlib import Path

from rich.console import Console
from rich.progress import track

from jm_downloader.config import DownloaderConfig, load_config_from_yaml
from jm_downloader.db import JmDB
from jm_downloader.cbz_packer import CbzPacker
from jm_downloader.utils import setup_logging, clean_title_for_filename

console = Console()

def main():
    parser = argparse.ArgumentParser(description='JM Repacker - Repack existing folders with new metadata')
    parser.add_argument('--config', '-c', help='YAML 配置文件路径', default=None)
    args = parser.parse_args()

    # Load Config to get paths
    cfg_data = load_config_from_yaml(args.config)
    cfg = DownloaderConfig(
        out_dir=Path(cfg_data.get('out_dir', './downloads')),
        retries=int(cfg_data.get('retries', 3)),
        delete_after_pack=bool(cfg_data.get('delete_after_pack', False)),
        extract_title=bool(cfg_data.get('extract_title', False)),
        jm_option_file=Path(cfg_data['jm_option_file']) if cfg_data.get('jm_option_file') else None,
        username=args.username or cfg_data.get('username'),
        password=args.password or cfg_data.get('password'),
        download_favorites=False,
        album_ids=[],
        save_db=Path(cfg_data.get('save_db', './downloads_db.sqlite'))
    )

    setup_logging(cfg.out_dir / 'repacker.log')
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

    originals_dir = cfg.out_dir / 'originals'
    if not originals_dir.exists():
        console.log(f"[red]找不到原来的图片目录: {originals_dir}[/red]")
        return

    count = 0
    
    console.log(f"[blue]开始扫描 {len(books)} 本已记录的书籍...[/blue]")
    
    for aid, book in track(books.items(), description="Repacking..."):
        raw_title = book['title']

        candidates = set()
        # New strict limit
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=True, max_len=180))
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=False, max_len=180))
        # Legacy limit (default was 200 before)
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=True, max_len=200))
        candidates.add(clean_title_for_filename(raw_title, extract_brackets=False, max_len=200))
        
        found_path = None
        for cand in candidates:
            p = originals_dir / cand
            if p.exists() and p.is_dir():
                found_path = p
                break
        
        if not found_path:
            continue

        cbz_base = cfg.out_dir / 'cbz' / found_path.name
        cbz_base.mkdir(parents=True, exist_ok=True)
        authors_str = book['author'] 
        tags_str = book['tags']
        summary = book['description']
        # Full series name for metadata
        cbz_series = clean_title_for_filename(raw_title, extract_brackets=cfg.extract_title, max_len=999)

        for chap_dir in found_path.iterdir():
            if not chap_dir.is_dir():
                continue
                
            chap_name = chap_dir.name

            import re
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
            except Exception as e:
                console.print(f"[red]打包失败 {found_path.name}/{chap_name}: {e}[/red]")
        
        count += 1

    console.log(f"[green]重打包完成，共处理 {count} 本[/green]")

if __name__ == '__main__':
    main()
