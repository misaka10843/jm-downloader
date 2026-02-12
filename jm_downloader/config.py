import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any


@dataclass
class DownloaderConfig:
    out_dir: Path = Path("./downloads")
    retries: int = 3
    delete_after_pack: bool = False
    extract_title: bool = False
    session_timeout: int = 20
    save_db: Path = Path("./downloads_db.sqlite")
    jm_option_file: Optional[Path] = None
    username: Optional[str] = None
    password: Optional[str] = None
    download_favorites: bool = True
    album_ids: List[str] = field(default_factory=list)

    def ensure_dirs(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "originals").mkdir(parents=True, exist_ok=True)
        (self.out_dir / "cbz").mkdir(parents=True, exist_ok=True)
        if not self.save_db.exists():
            with open(self.save_db, "w", encoding="utf-8") as f:
                json.dump({}, f)


def load_config_from_yaml(path: str) -> Dict[str, Any]:
    import yaml, pathlib
    if not path:
        return {}
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
