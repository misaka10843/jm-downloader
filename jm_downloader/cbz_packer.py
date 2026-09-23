from dataclasses import fields as _dataclass_fields
from pathlib import Path
from typing import Optional

from cbz.comic import ComicInfo
from cbz.constants import PageType, Format
from cbz.page import PageInfo

try:
    # ComicInfo 是 dataclass：只有这里列出的名字才会被 pack() 写进 ComicInfo.xml
    _COMIC_FIELDS = {f.name for f in _dataclass_fields(ComicInfo)}
except TypeError:          # 库换实现时退化为不做校验，不影响打包
    _COMIC_FIELDS = set()


def _apply(comic, **values) -> None:
    """按字段名写元数据，字段名不存在就直接报错。

    给 dataclass 实例赋一个不存在的属性不会报错，只是挂个野属性，pack() 时静默丢掉 ——
    历史上 `comic.authors = ...` 就是这样，库里的字段其实叫 `writer`，
    导致所有生成的 CBZ 都没有作者信息且毫无报错。
    """
    for name, value in values.items():
        if not value:
            continue
        if _COMIC_FIELDS and name not in _COMIC_FIELDS:
            raise ValueError(
                f'cbz 库的 ComicInfo 没有字段 {name!r}，可用的有: '
                f'{", ".join(sorted(_COMIC_FIELDS))}')
        setattr(comic, name, value)


class CbzPacker:
    @staticmethod
    def pack_images_to_cbz(images_folder: Path, cbz_path: Path, title: str, series: Optional[str],
                           number: Optional[float], authors: Optional[str] = None,
                           tags: Optional[str] = None, summary: Optional[str] = None,
                           album_id: Optional[str] = None) -> None:
        paths = sorted([p for p in images_folder.iterdir() if p.is_file()])
        if not paths:
            # 空目录照样能打出一个只有 ComicInfo.xml 的 cbz，调用方还会把它标记成
            # 「已打包」—— 于是这个章节永远是一本 0 页的空书，也不会再重试。
            raise ValueError(f'目录里没有任何文件，拒绝打包空书: {images_folder}')
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

        # 参数名 -> ComicInfo 标准字段名。作者在标准里叫 <Writer>，
        # 简介叫 <Summary>（<Notes> 是另一个字段，别混用）。
        _apply(comic, writer=authors, tags=tags, summary=summary)

        cbz_bytes = comic.pack()
        cbz_path.write_bytes(cbz_bytes)
