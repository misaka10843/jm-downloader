import logging
import random
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List

import jmcomic
import requests
from jmcomic import JmOption, JmApiClient, ResponseUnexpectedException
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn, SpinnerColumn

from .cbz_packer import CbzPacker
from .db import JmDB
from .search import album_cover_url
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
            time.sleep(random.randint(1, 3))
            if username and password:
                try:
                    self.login(username, password)
                    console.log(f"[green][Auto-Relogin] 重新登录成功，正在重新请求...[/green]")
                    time.sleep(random.randint(1, 3))
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


def create_client(cfg):
    """按配置创建（并按需登录）jmcomic 客户端。

    供下载器与搜索/检查更新等只读命令共用，避免重复实现登录逻辑。
    """
    if cfg.jm_option_file:
        option = jmcomic.create_option_by_file(str(cfg.jm_option_file))
    else:
        option = JmOption.default()
    client = option.new_jm_client()
    if cfg.username and cfg.password:
        try:
            client.login(cfg.username, cfg.password)
            client._username = cfg.username
            client._password_for_relogin = cfg.password
            console.log('[green]登录成功[/green]')
        except Exception as e:
            console.log(f'[red]登录失败: {e}[/red]')
    return client


class JmFavDownloader:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.db = JmDB(cfg.save_db)
        self.client = create_client(cfg)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'jm_fav_downloader_modular/1.0'})
        self.session_timeout = cfg.session_timeout

    def get_favorites_album_ids(self) -> List[str]:
        if not self.cfg.download_favorites:
            return []

        console.log('[blue]正在检查收藏夹更新...[/blue]')

        cached_list = self.db.get_fav_list() or []
        cached_latest_id = self.db.get_fav_latest_id()

        stop_marker_ids = set(cached_list[:3])

        new_album_ids = []
        latest_online_id = None
        reached_cache = False

        with Progress(SpinnerColumn(), TextColumn('[progress.description]{task.description}'), console=console) as prog:
            task = prog.add_task('获取收藏中...', total=None)
            try:
                for page in self.client.favorite_folder_gen():
                    for aid, title in page.iter_id_title():
                        aid_str = str(aid)

                        if latest_online_id is None:
                            latest_online_id = aid_str
                            if latest_online_id == cached_latest_id:
                                reached_cache = True
                                break

                        if aid_str in stop_marker_ids:
                            reached_cache = True
                            break

                        if aid_str not in new_album_ids:
                            new_album_ids.append(aid_str)

                    prog.update(task, description=f'已收集新收藏: {len(new_album_ids)} 本')

                    if reached_cache:
                        break

            except Exception as e:
                console.log(f'[red]获取收藏夹失败: {e}[/red]')
                return cached_list

        if not latest_online_id:
            console.log('[yellow]收藏夹为空 or 获取失败[/yellow]')
            return cached_list

        if len(new_album_ids) == 0:
            console.log(f'[green]收藏夹无变化 (最新ID: {latest_online_id})，使用缓存列表[/green]')
            return cached_list

        console.log(f'[blue]发现新收藏，新增了 {len(new_album_ids)} 本，正在与本地缓存合并...[/blue]')

        final_album_ids = new_album_ids.copy()
        for aid in cached_list:
            if aid not in final_album_ids:
                final_album_ids.append(aid)

        self.db.set_fav_latest_id(latest_online_id)
        self.db.set_fav_list(final_album_ids)

        return final_album_ids

    def _cache_album(self, album):
        """把本子元数据写进 books，并顺手补上封面 / 页数。

        详情对象已经在手上，取封面和页数**不需要额外请求**。
        作者选择页正是靠这些封面来分辨同名 / 多写法作者的，所以要在这里落库，
        否则存量库全是空封面（可用 `cli.py ui` 的补全逻辑兜底）。
        """
        self.db.save_book(album)
        aid = str(getattr(album, 'album_id', getattr(album, 'id', None)) or '')
        if not aid:
            return
        try:
            self.db.set_book_extras(
                aid,
                cover_url=album_cover_url(aid),
                page_count=getattr(album, 'page_count', None),
            )
        except Exception as e:          # noqa: BLE001 - 封面只是锦上添花，不该中断下载
            log.debug('补全 %s 的封面/页数失败: %s', aid, e)
            # 没写进去就别标记「已尝试」，否则 `cli.py ui` 永远不会再给它补封面
            return
        # 详情刚刚就在手上，标记为已尝试，免得 `cli.py ui` 再为它发一次请求
        self.db.mark_extras_checked(aid)

    def download_album_list(self, album_ids: List[str]):
        if not album_ids:
            console.log('[yellow]没有 album id 可下载[/yellow]')
            return
        from rich.table import Table
        table = Table('序号', 'album_id', 'title', '状态')

        # 建表格时已经拉到详情的本子，第二次循环直接复用，别重复请求
        prefetched: Dict[str, object] = {}
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
                    prefetched[aid] = album
                    raw = getattr(album, 'title', str(aid))
                    self._cache_album(album)  # Save to cache
                except Exception:
                    raw = str(aid)

            cleaned = clean_title_for_filename(raw, extract_brackets=self.cfg.extract_title)
            table.add_row(str(i), str(aid), cleaned)
        console.print(table)
        for aid in album_ids:
            if self.db.is_album_completed(aid):
                console.log(f"[green]本子 {aid} 已标记为完成，跳过下载[/green]")
                continue

            album = prefetched.get(aid)
            if album is None:
                try:
                    album = self.client.get_album_detail(aid)
                    self._cache_album(album)
                except Exception as e:
                    console.log(f'[red]获取本子 {aid} 详情失败: {e}[/red]')
                    continue
            self._download_album(album)

    def _download_album(self, album):
        album_id = str(getattr(album, 'album_id', getattr(album, 'id', None) or 'unknown'))
        raw_album_title = getattr(album, 'title', f'album_{album_id}')
        cleaned_album_title = clean_title_for_filename(raw_album_title, extract_brackets=self.cfg.extract_title)
        # 目录名不含 album_id，同名会互相覆盖，交给 DB 消歧
        folder_name = self.db.resolve_album_dir_name(album_id, cleaned_album_title)
        originals_base = self.cfg.originals_root / folder_name
        cbz_base = self.cfg.cbz_root / folder_name
        originals_base.mkdir(parents=True, exist_ok=True)
        cbz_base.mkdir(parents=True, exist_ok=True)
        if folder_name != cleaned_album_title:
            console.log(f'[yellow]目录名 {cleaned_album_title!r} 已被其它本子占用，'
                        f'本子 {album_id} 改用 {folder_name!r}[/yellow]')
        console.rule(f'处理本子: {folder_name} ({album_id})')
        all_photos = list(album)
        total_photos = len(all_photos)

        album_failed = False

        for idx, photo_summary in enumerate(all_photos, start=1):
            # photo_id 在 summary 上就有，先用它判一次「已打包」：否则每个已完成的章节
            # 都会白发一次详情请求。失败章节会让整个本子留在待办里，重跑时这个浪费很可观。
            summary_pid = str(getattr(photo_summary, 'photo_id', None) or '')
            if summary_pid and self.db.is_packed(album_id, summary_pid):
                console.log(f"[blue]已打包，跳过: {cleaned_album_title} / {summary_pid}[/blue]")
                continue
            try:
                photo = self.client.get_photo_detail(photo_summary.photo_id, False)
            except Exception as e:
                # 不能退回 photo_summary：album 迭代出来的是 JmPhotoDetail 壳
                # （create_photo_detail 不传 page_arr），它 len()/迭代都抛 TypeError，
                # 拿它当详情会让整个下载批次直接崩掉。这里只判本章失败，下次再试。
                console.log(f'[red]获取章节详情失败，跳过本章: {photo_summary.photo_id} ({e})[/red]')
                album_failed = True
                continue
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
            photo_id = str(getattr(photo, 'photo_id', getattr(photo, 'id', None) or f"{album_id}_{chap_num}"))
            # 先判断是否已打包，再创建目录：否则开启 delete_after_pack 后每次重跑都会重建空目录
            if self.db.is_packed(album_id, photo_id) or self.db.is_packed(album_id, file_chapter_name):
                console.log(f"[blue]已打包，跳过: {cleaned_album_title} / {display_title}[/blue]")
                continue
            photo_folder = originals_base / f"{file_chapter_name}"
            photo_folder.mkdir(parents=True, exist_ok=True)
            image_list = list(photo)
            if not image_list:
                console.log(f"[yellow]无图片，跳过: {display_title}[/yellow]")
                # 别留下刚建的空目录：repacker 会把空目录打成没有页的 cbz 并标记已打包
                try:
                    photo_folder.rmdir()
                except OSError:
                    pass
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
                # 优先用 authors（完整列表）：JmAlbumDetail.author 只返回 authors[0]，
                # 用它会让 cbz 的 <Writer> 丢掉所有合作作者。
                authors_raw = getattr(album, 'authors', None) or getattr(album, 'author', None)
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
                # 同时以 photo_id 与章节目录名入库，使 repacker 打包过的章节也不会被重复下载
                self.db.mark_packed(album_id, photo_id)
                self.db.mark_packed(album_id, file_chapter_name)
                if self.cfg.delete_after_pack:
                    shutil.rmtree(photo_folder, ignore_errors=True)
                    console.log(f"[grey]已删除原图文件夹: {photo_folder}[/grey]")
            except Exception as e:
                console.log(f"[red]CBZ 打包失败: {e}[/red]")
                album_failed = True

        if not album_failed and total_photos > 0:
            self.db.mark_album_completed(album_id)
            console.log(f"[bold green]本子 {album_id} 全部章节处理完毕，标记为完成[/bold green]")

        self._cleanup_empty_album_dir(originals_base)

    def _cleanup_empty_album_dir(self, originals_base: Path):
        """开启 delete_after_pack 后，若本子目录下的章节已全部删除，则一并清掉这个空目录"""
        if not self.cfg.delete_after_pack or not self.cfg.delete_empty_album_dir:
            return
        try:
            if originals_base.exists() and not any(originals_base.iterdir()):
                originals_base.rmdir()
                console.log(f"[grey]已删除空的原始目录: {originals_base}[/grey]")
        except OSError as e:
            log.debug(f"清理空目录失败 {originals_base}: {e}")
