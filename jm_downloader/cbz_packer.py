from pathlib import Path
from typing import List, Optional

from cbz.comic import ComicInfo
from cbz.constants import PageType, Format
from cbz.page import PageInfo


class CbzPacker:
    @staticmethod
    def pack_images_to_cbz(images_folder: Path, cbz_path: Path, title: str, series: Optional[str],
                           number: Optional[float], authors: Optional[str] = None,
                           tags: Optional[str] = None, summary: Optional[str] = None,
                           album_id: Optional[str] = None) -> None:
        paths = sorted([p for p in images_folder.iterdir() if p.is_file()])
        pages = []
        for i, p in enumerate(paths):
            pt = PageType.FRONT_COVER if i == 0 else PageType.BACK_COVER if i == len(paths) - 1 else PageType.STORY
            pages.append(PageInfo.load(path=p, type=pt))

        kwargs = {
            'title': title,
            'series': series or title,
            'number': number or 1,
            'format': Format.WEB_COMIC,
            'web': f"https://18comic.vip/album/{album_id}" if album_id else None
        }
        comic = ComicInfo.from_pages(pages=pages, **{k: v for k, v in kwargs.items() if v is not None})
        if authors:
            comic.authors = authors
        if tags:
            comic.tags = tags
        if summary:
            comic.notes = summary
        cbz_bytes = comic.pack()
        cbz_path.write_bytes(cbz_bytes)
