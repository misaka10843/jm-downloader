from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any


@dataclass
class DownloaderConfig:
    out_dir: Path = Path("./downloads")
    retries: int = 3
    delete_after_pack: bool = False
    delete_empty_album_dir: bool = True
    extract_title: bool = False
    session_timeout: int = 20
    save_db: Path = Path("./downloads_db.sqlite")
    jm_option_file: Optional[Path] = None
    username: Optional[str] = None
    password: Optional[str] = None
    download_favorites: bool = True
    album_ids: List[str] = field(default_factory=list)
    # 为 None 时分别回退到 out_dir/cbz 与 out_dir/originals
    cbz_dir: Optional[Path] = None
    originals_dir: Optional[Path] = None

    @property
    def cbz_root(self) -> Path:
        """CBZ 打包产物根目录"""
        return Path(self.cbz_dir) if self.cbz_dir else Path(self.out_dir) / "cbz"

    @property
    def originals_root(self) -> Path:
        """原始图片根目录"""
        return Path(self.originals_dir) if self.originals_dir else Path(self.out_dir) / "originals"

    def ensure_dirs(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.originals_root.mkdir(parents=True, exist_ok=True)
        self.cbz_root.mkdir(parents=True, exist_ok=True)
        # save_db 由 JmDB 自行初始化，这里只保证父目录存在
        db_parent = Path(self.save_db).parent
        if str(db_parent):
            db_parent.mkdir(parents=True, exist_ok=True)


# 未显式指定 -c 时默认尝试读取的配置文件
DEFAULT_CONFIG_FILE = "./config.yml"

_TRUE_WORDS = {"true", "yes", "y", "on", "1"}
_FALSE_WORDS = {"false", "no", "n", "off", "0"}


def resolve_config_path(explicit: Optional[str]) -> Optional[str]:
    """返回要读取的配置文件路径：``-c`` 优先，否则当前目录存在 ``./config.yml`` 就用它。"""
    if explicit:
        return explicit
    default = Path(DEFAULT_CONFIG_FILE)
    if default.exists():
        return str(default)
    return None


def load_config_from_yaml(path) -> Dict[str, Any]:
    """读取 YAML 配置，返回值一定是 dict。

    失败必须响亮：静默返回 ``{}`` 会让用户以为配置生效了，实际下到默认目录。
    """
    import yaml
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到配置文件: {p}")
    if p.is_dir():
        raise IsADirectoryError(f"配置文件路径是个目录: {p}")

    with p.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"配置文件 {p} 不是合法的 YAML: {e}") from e

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"配置文件 {p} 的顶层必须是「键: 值」映射，实际读到 {type(data).__name__}"
            f"（是不是整体多了一层缩进，或漏了冒号？）")
    return data


def _opt_path(value, key: str = "路径") -> Optional[Path]:
    """空值 / 空字符串归一化为 None，避免生成 Path('.')"""
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        if not value.strip():
            return None
        return Path(value)
    raise ValueError(f"配置项 {key} 需要字符串路径，实际读到 {type(value).__name__}")


def _path_or(value, default: str, key: str) -> Path:
    """有默认值的路径项：空值回退默认，类型写错则点名报错（而不是 Path() 抛 TypeError）。"""
    p = _opt_path(value, key)
    return p if p is not None else Path(default)


def _as_bool(value, default: bool, key: str) -> bool:
    """严格解析布尔值（不能用 bool()：bool('no') is True 会让原图被误删）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_WORDS:
            return True
        if v in _FALSE_WORDS:
            return False
        raise ValueError(f"配置项 {key} 需要布尔值，实际读到 {value!r}（可用 true/false）")
    raise ValueError(f"配置项 {key} 需要布尔值，实际读到 {type(value).__name__}")


def _as_int(value, default: int, key: str, minimum: Optional[int] = None) -> int:
    """严格解析整数；写错时给出「哪个配置项写错了」而不是裸 ValueError。

    ``minimum`` 用来拦住会让功能静默失效的取值：``retries: 0`` 会让
    ``range(1, retries + 1)`` 变成空循环 —— 一张图都不尝试，全部章节判失败。
    """
    if value is None:
        n = default
    elif isinstance(value, bool):
        raise ValueError(f"配置项 {key} 需要整数，实际读到布尔值 {value!r}")
    elif isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if value != int(value):
            raise ValueError(f"配置项 {key} 需要整数，实际读到 {value!r}")
        n = int(value)
    elif isinstance(value, str):
        try:
            n = int(value.strip())
        except ValueError:
            raise ValueError(f"配置项 {key} 需要整数，实际读到 {value!r}") from None
    else:
        raise ValueError(f"配置项 {key} 需要整数，实际读到 {type(value).__name__}")
    if minimum is not None and n < minimum:
        raise ValueError(f"配置项 {key} 不能小于 {minimum}，实际读到 {n}")
    return n


def _as_id_list(value, key: str) -> List[str]:
    """解析本子 id 列表；单个 id 要当整体，不能 list('422866') 拆成 6 个。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, (str, int)):
        s = str(value).strip()
        return [s] if s else []
    raise ValueError(f"配置项 {key} 需要 id 或 id 列表，实际读到 {type(value).__name__}")


def build_config(cfg_data: Dict[str, Any], *, username: Optional[str] = None,
                 password: Optional[str] = None, download_favorites: Optional[bool] = None,
                 album_ids: Optional[List[str]] = None) -> DownloaderConfig:
    """由 YAML 数据 + CLI 覆盖项统一构建配置，避免各处重复解析导致字段遗漏

    ``download_favorites`` / ``album_ids`` 传 None 表示「命令行没说」，此时才读配置文件。
    """
    if not isinstance(cfg_data, dict):
        raise ValueError(f"配置数据必须是 dict，实际是 {type(cfg_data).__name__}")
    return DownloaderConfig(
        out_dir=_path_or(cfg_data.get('out_dir'), './downloads', 'out_dir'),
        cbz_dir=_opt_path(cfg_data.get('cbz_dir'), 'cbz_dir'),
        originals_dir=_opt_path(cfg_data.get('originals_dir'), 'originals_dir'),
        retries=_as_int(cfg_data.get('retries'), 3, 'retries', minimum=1),
        delete_after_pack=_as_bool(cfg_data.get('delete_after_pack'), False, 'delete_after_pack'),
        delete_empty_album_dir=_as_bool(cfg_data.get('delete_empty_album_dir'), True,
                                        'delete_empty_album_dir'),
        extract_title=_as_bool(cfg_data.get('extract_title'), False, 'extract_title'),
        session_timeout=_as_int(cfg_data.get('session_timeout'), 20, 'session_timeout', minimum=1),
        save_db=_path_or(cfg_data.get('save_db'), './downloads_db.sqlite', 'save_db'),
        jm_option_file=_opt_path(cfg_data.get('jm_option_file'), 'jm_option_file'),
        username=username or cfg_data.get('username'),
        password=password or cfg_data.get('password'),
        download_favorites=(_as_bool(cfg_data.get('download_favorites'), True, 'download_favorites')
                            if download_favorites is None else bool(download_favorites)),
        album_ids=(_as_id_list(album_ids, 'album_ids')
                   or _as_id_list(cfg_data.get('album_ids'), 'album_ids')),
    )
