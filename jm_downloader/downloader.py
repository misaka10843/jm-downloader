import logging
import re
import shutil
from pathlib import Path
from typing import List

import jmcomic
import requests
from jmcomic import JmOption, JmApiClient, ResponseUnexpectedException
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, SpinnerColumn

from .cbz_packer import CbzPacker
from .db import JmDB
from .utils import clean_title_for_filename

console = Console()
log = logging.getLogger('jm_downloader')

original_req_api = JmApiClient.req_api


def req_api_with_auto_relogin(self, url, *args, **kwargs):
    try:
        return original_req_api(self, url, *args, **kwargs)
    except ResponseUnexpectedException as e:
        error_msg = str(e)
        if '401' in error_msg or '請先登入會員' in error_msg:
            console.log(f"[yellow][Auto-Relogin] 检测到登录失败 (401)，尝试重新登录...[/yellow]")

            username = getattr(self, '_username', None)
            password = getattr(self, '_password_for_relogin', None)

            if username and password:
                try:
                    self.login(username, password)
                    console.log(f"[green][Auto-Relogin] 重新登录成功，正在重新请求...[/green]")
                    return original_req_api(self, url, *args, **kwargs)
                except Exception as login_e:
                    console.log(f"[red][Auto-Relogin] 重新登录失败: {login_e}[/red]")
                    raise e
            else:
                console.log(
                    f"[red][Auto-Relogin] 无法重试登录 (username:{username},password:{password})，请检查账号密码。[/red]")
                raise e
        else:
            raise e


def rich_logging_executor(topic: str, msg: str):
    console.log(f"[jmcomic][[topic]{topic}[/topic]] [msg]{msg}[/msg]")


# 覆盖jmcomic的print日志
jmcomic.JmModuleConfig.EXECUTOR_LOG = rich_logging_executor

# 覆盖api请求方法
JmApiClient.req_api = req_api_with_auto_relogin


class JmFavDownloader:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.db = JmDB(cfg.save_db)
        if cfg.jm_option_file:
            option = jmcomic.create_option_by_file(str(cfg.jm_option_file))
        else:
            option = JmOption.default()
        self.client = option.new_jm_client()
        if cfg.username and cfg.password:
            try:
                self.client.login(cfg.username, cfg.password)
                self.client._username = cfg.username
                self.client._password_for_relogin = cfg.password
                console.log('[green]登录成功[/green]')
            except Exception as e:
                console.log(f'[red]登录失败: {e}[/red]')
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'jm_fav_downloader_modular/1.0'})
        self.session_timeout = cfg.session_timeout

    def get_favorites_album_ids(self) -> List[str]:
        if not self.cfg.download_favorites:
            return []

        console.log('[blue]正在检查收藏夹更新...[/blue]')
        latest_online_id = None
        try:
            gen = self.client.favorite_folder_gen()
            first_page_ids = []
            for page in gen:
                for aid, _ in page.iter_id_title():
                    first_page_ids.append(str(aid))
                break

            if first_page_ids:
                latest_online_id = first_page_ids[0]
        except Exception as e:
            console.log(f'[red]获取收藏夹失败: {e}[/red]')
            return []

        if not latest_online_id:
            console.log('[yellow]收藏夹为空 or 获取失败[/yellow]')
            return []

        cached_latest_id = self.db.get_fav_latest_id()
        cached_list = self.db.get_fav_list()

        if cached_latest_id == latest_online_id and cached_list:
            console.log(f'[green]收藏夹无变化 (最新ID: {latest_online_id})，使用缓存列表[/green]')
            return cached_list

        console.log(f'[blue]发现新收藏 (旧: {cached_latest_id}, 新: {latest_online_id})，重新获取全部收藏...[/blue]')

        album_ids = []
        with Progress(SpinnerColumn(), TextColumn('[progress.description]{task.description}'), console=console) as prog:
            task = prog.add_task('获取收藏中...', total=None)
            try:
                for page in self.client.favorite_folder_gen():
                    for aid, title in page.iter_id_title():
                        if str(aid) not in album_ids:
                            album_ids.append(str(aid))
                    prog.update(task, description=f'已收集: {len(album_ids)} 本')
                prog.update(task, description=f'收藏抓取完成，共 {len(album_ids)} 本')

                # 更新缓存
                self.db.set_fav_latest_id(latest_online_id)
                self.db.set_fav_list(album_ids)

            except Exception as e:
                console.log(f'[red]获取收藏夹失败: {e}[/red]')
                return []
        return album_ids

    def download_album_list(self, album_ids: List[str]):
        if not album_ids:
            console.log('[yellow]没有 album id 可下载[/yellow]')
            return
        from rich.table import Table
        table = Table('序号', 'album_id', 'title', '状态')

        for i, aid in enumerate(album_ids, 1):
            if self.db.is_album_completed(aid):
                cached_book = self.db.get_book(aid)
                title = cached_book['title'] if cached_book else str(aid)

                table.add_row(str(i), str(aid), title, "[green]已完成 (跳过)[/green]")
                continue

            cached_book = self.db.get_book(aid)
            if cached_book:
                raw = cached_book['title']
            else:
                try:
                    album = self.client.get_album_detail(aid)
                    raw = getattr(album, 'title', str(aid))
                    self.db.save_book(album)  # Save to cache
                except Exception:
                    raw = str(aid)

            cleaned = clean_title_for_filename(raw, extract_brackets=self.cfg.extract_title)
            table.add_row(str(i), str(aid), cleaned)
        console.print(table)
        for aid in album_ids:
            if self.db.is_album_completed(aid):
                console.log(f"[green]本子 {aid} 已标记为完成，跳过下载[/green]")
                continue

            try:
                cached_book = self.db.get_book(aid)
                if cached_book:
                    album = self.client.get_album_detail(aid)
                    self.db.save_book(album)
                else:
                    album = self.client.get_album_detail(aid)
                    self.db.save_book(album)
            except Exception as e:
                console.log(f'[red]获取本子 {aid} 详情失败: {e}[/red]')
                continue
            self._download_album(album)

    def _download_album(self, album):
        album_id = str(getattr(album, 'album_id', getattr(album, 'id', None) or 'unknown'))
        raw_album_title = getattr(album, 'title', f'album_{album_id}')
        cleaned_album_title = clean_title_for_filename(raw_album_title, extract_brackets=self.cfg.extract_title)
        originals_base = self.cfg.out_dir / 'originals' / cleaned_album_title
        cbz_base = self.cfg.out_dir / 'cbz' / cleaned_album_title
        originals_base.mkdir(parents=True, exist_ok=True)
        cbz_base.mkdir(parents=True, exist_ok=True)
        console.rule(f'处理本子: {cleaned_album_title} ({album_id})')
        all_photos = list(album)
        total_photos = len(all_photos)

        album_failed = False

        for idx, photo_summary in enumerate(all_photos, start=1):
            try:
                photo = self.client.get_photo_detail(photo_summary.photo_id, False)
            except Exception:
                photo = photo_summary
            try:
                chap_num = int(getattr(photo, 'sort', getattr(photo, 'index', None) or idx))
            except Exception:
                chap_num = idx
            raw_photo_title = getattr(photo, 'title', '') or ''
            is_custom_title = False
            cleaned_photo_title = ''
            if raw_photo_title:
                if not re.match(r'^(chapter_|chapter|photo_|photo)', raw_photo_title, flags=re.I):
                    is_custom_title = True
                    cleaned_photo_title = clean_title_for_filename(raw_photo_title,
                                                                   extract_brackets=self.cfg.extract_title)
            file_chapter_name = f'第{chap_num}话'
            if is_custom_title and chap_num > 1:
                display_title = f"{file_chapter_name} - {cleaned_photo_title}"
            else:
                display_title = file_chapter_name
            photo_folder = originals_base / f"{file_chapter_name}"
            photo_folder.mkdir(parents=True, exist_ok=True)
            photo_id = str(getattr(photo, 'photo_id', getattr(photo, 'id', None) or f"{album_id}_{chap_num}"))
            if self.db.is_packed(album_id, photo_id):
                console.log(f"[blue]已打包，跳过: {cleaned_album_title} / {display_title}[/blue]")
                continue
            image_list = list(photo)
            if not image_list:
                console.log(f"[yellow]无图片，跳过: {display_title}[/yellow]")
                continue
            with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    "[progress.percentage]{task.percentage:>3.0f}%",
                    TimeElapsedColumn(),
                    TimeRemainingColumn(),
                    console=console
            ) as pr:
                task_desc = f"{cleaned_album_title} / {file_chapter_name}"
                task = pr.add_task(task_desc, total=len(image_list))
                failed = False
                for i_img, img in enumerate(image_list, start=1):
                    img_url = getattr(img, 'img_url', None)
                    suffix = Path(img_url).suffix if img_url else '.jpg'
                    out_name = f"{i_img:04d}{suffix}"
                    out_path = photo_folder / out_name
                    if out_path.exists():
                        pr.update(task, advance=1)
                        continue
                    ok = False
                    for attempt in range(1, self.cfg.retries + 1):
                        try:
                            try:
                                self.client.download_by_image_detail(img, str(out_path))
                            except Exception:
                                resp = self.session.get(img_url, timeout=self.session_timeout)
                                resp.raise_for_status()
                                out_path.write_bytes(resp.content)
                            ok = True
                            break
                        except Exception as e:
                            console.log(f"[yellow]图片下载失败 ({attempt}/{self.cfg.retries}): {e}[/yellow]")
                            import time;
                            time.sleep(0.5)
                    if not ok:
                        console.log(f"[red]图片多次失败，标记本章失败: {img_url}[/red]")
                        failed = True
                    pr.update(task, advance=1)
            cbz_target = cbz_base / f"{file_chapter_name}.cbz"
            if failed:
                console.log(f"[red]章节下载存在失败，跳过 CBZ 打包: {file_chapter_name}[/red]")
                album_failed = True
                continue
            authors_str = None
            tags_str = None
            summary = None
            try:
                authors_raw = getattr(album, 'author', None) or getattr(album, 'authors', None)
                author_list = []
                if authors_raw:
                    if isinstance(authors_raw, str):
                        author_list = [clean_title_for_filename(a.strip(), extract_brackets=True) for a in
                                       [authors_raw]]
                    elif isinstance(authors_raw, list):
                        author_list = [clean_title_for_filename(a, extract_brackets=True) for a in authors_raw]

                # Filter unknown
                valid_authors = []
                for a in author_list:
                    if a and a.lower() not in ('unknown', 'none', '未知', 'default_author'):
                        valid_authors.append(a)
                if valid_authors:
                    authors_str = ','.join(valid_authors)

                tags = getattr(album, 'tags', None)
                if tags:
                    if isinstance(tags, list):
                        tags_str = ','.join(tags)
                    else:
                        tags_str = str(tags)

                summary = getattr(album, 'description', None) or getattr(album, 'summary', None)
            except Exception:
                pass
            cbz_title = f"{display_title}"
            cbz_series = clean_title_for_filename(raw_album_title, extract_brackets=self.cfg.extract_title, max_len=999)

            try:
                CbzPacker.pack_images_to_cbz(images_folder=photo_folder, cbz_path=cbz_target,
                                             title=cbz_title, series=cbz_series, number=chap_num,
                                             authors=authors_str, tags=tags_str, summary=summary,
                                             album_id=album_id)
                console.log(f"[green]打包完成: {cbz_target}[/green]")
                self.db.mark_packed(album_id, photo_id)
                if self.cfg.delete_after_pack:
                    shutil.rmtree(photo_folder, ignore_errors=True)
                    console.log(f"[grey]已删除原图文件夹: {photo_folder}[/grey]")
            except Exception as e:
                console.log(f"[red]CBZ 打包失败: {e}[/red]")
                album_failed = True

        if not album_failed and total_photos > 0:
            self.db.mark_album_completed(album_id)
            console.log(f"[bold green]本子 {album_id} 全部章节处理完毕，标记为完成[/bold green]")
