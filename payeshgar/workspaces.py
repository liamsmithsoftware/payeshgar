from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from modules.telegram_app.collectors.manager import SyncManager as TgSyncManager
from modules.telegram_app.collectors.telethon_client import TelethonCollector
from modules.telegram_app.collectors.web_scraper import WebScraperCollector
from modules.telegram_app.database import DatabaseManager as TgDatabaseManager
from modules.telegram_app.services.scheduler import ChannelSchedulerService as TgSchedulerService
from modules.web_app.collectors.manager import SyncManager as WebSyncManager
from modules.web_app.collectors.web_collector import WebSiteCollector
from modules.web_app.database import DatabaseManager as WebDatabaseManager
from modules.web_app.services.scheduler import SiteSchedulerService as WebSchedulerService
from modules.x_app.collectors.manager import SyncManager as XSyncManager
from modules.x_app.collectors.x_web import XWebCollector
from modules.x_app.database import DatabaseManager as XDatabaseManager
from modules.x_app.services.scheduler import ChannelSchedulerService as XSchedulerService

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACES_ROOT = Path(
    os.getenv("PAYESHGAR_WORKSPACES_DIR", str(ROOT_DIR / "data" / "workspaces"))
)
LEGACY_SHARED_WORKSPACE = ROOT_DIR / "data" / "workspace"


@dataclass
class ModuleRuntime:
    db: Any
    sync_manager: Any
    scheduler: Any


class WorkspaceRegistry:
    """
    Manages isolated per-user workspaces (Web, Telegram, and X) for every user in Payeshgar.
    Each user has dedicated SQLite files, sessions, sync locks, and schedulers under:
      data/workspaces/user_<id>/{web,telegram,x}/
    """

    def __init__(
        self,
        *,
        workspaces_root: Optional[Path] = None,
        enable_schedulers: bool = True,
    ) -> None:
        self.root = Path(workspaces_root) if workspaces_root else DEFAULT_WORKSPACES_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.enable_schedulers = enable_schedulers

        # Shared stateless HTTP/browser collectors; state lives per-user in DB/Telethon session
        self._web_collector = WebSiteCollector()
        self._tg_web_collector = WebScraperCollector()
        self._x_collector = XWebCollector()

        self._bundles: Dict[Tuple[int, str], ModuleRuntime] = {}
        self._running = False

    def user_dir(self, user_id: int) -> Path:
        return self.root / f"user_{int(user_id)}"

    def _maybe_migrate_legacy_admin_data(self, user_id: int) -> None:
        """Preserve any existing admin data from legacy `data/workspace/` into `user_1/`."""
        if int(user_id) != 1 or self.root != DEFAULT_WORKSPACES_ROOT:
            return
        if not LEGACY_SHARED_WORKSPACE.exists():
            return
        u_dir = self.user_dir(1)
        mapping = [
            (LEGACY_SHARED_WORKSPACE / "web" / "web_collector.sqlite", u_dir / "web" / "web_collector.sqlite"),
            (LEGACY_SHARED_WORKSPACE / "telegram" / "telegram_collector.sqlite", u_dir / "telegram" / "telegram_collector.sqlite"),
            (LEGACY_SHARED_WORKSPACE / "x" / "x_collector.sqlite", u_dir / "x" / "x_collector.sqlite"),
        ]
        for src, dst in mapping:
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                for ext in ("-wal", "-shm"):
                    sidecar = Path(str(src) + ext)
                    if sidecar.exists():
                        shutil.copy2(sidecar, Path(str(dst) + ext))

    def _can_start_scheduler_now(self) -> bool:
        if not self.enable_schedulers or not self._running:
            return False
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def get_runtime(self, user_id: int, mod: str) -> ModuleRuntime:
        uid = int(user_id)
        key = (uid, mod)
        existing = self._bundles.get(key)
        if existing is not None:
            if self._can_start_scheduler_now() and not existing.scheduler.is_running:
                existing.scheduler.start()
            return existing

        self._maybe_migrate_legacy_admin_data(uid)
        base = self.user_dir(uid) / mod
        (base / "exports").mkdir(parents=True, exist_ok=True)

        if mod == "web":
            db = WebDatabaseManager(db_path=base / "web_collector.sqlite")
            sync_mgr = WebSyncManager(db=db, web_collector=self._web_collector)
            sched = WebSchedulerService(db=db, sync_manager=sync_mgr)
        elif mod == "telegram":
            db = TgDatabaseManager(db_path=base / "telegram_collector.sqlite")
            t_col = TelethonCollector(session_prefix=base / "telethon_session")
            sync_mgr = TgSyncManager(
                db=db,
                web_collector=self._tg_web_collector,
                telethon_collector=t_col,
            )
            sched = TgSchedulerService(db=db, sync_manager=sync_mgr)
        elif mod == "x":
            db = XDatabaseManager(db_path=base / "x_collector.sqlite")
            sync_mgr = XSyncManager(db=db, web_collector=self._x_collector)
            sched = XSchedulerService(db=db, sync_manager=sync_mgr)
        else:
            raise ValueError(f"Unknown workspace module: {mod}")

        if self._can_start_scheduler_now():
            sched.start()

        runtime = ModuleRuntime(db=db, sync_manager=sync_mgr, scheduler=sched)
        self._bundles[key] = runtime
        return runtime

    def start(self, initial_user_ids: Iterable[int] = (1,)) -> None:
        self._running = True
        for uid in initial_user_ids:
            u_dir = self.user_dir(uid)
            # Always warm up admin (1) and any user that already has a workspace folder on disk
            if int(uid) == 1 or u_dir.exists():
                for mod in ("web", "telegram", "x"):
                    self.get_runtime(int(uid), mod)

    def stop_user(self, user_id: int) -> None:
        uid = int(user_id)
        for mod in ("web", "telegram", "x"):
            rt = self._bundles.pop((uid, mod), None)
            if rt is not None and rt.scheduler.is_running:
                rt.scheduler.stop()

    def stop_all(self) -> None:
        self._running = False
        for rt in list(self._bundles.values()):
            if rt.scheduler.is_running:
                rt.scheduler.stop()

    def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        uid = int(user_id)
        web_stats = self.get_runtime(uid, "web").db.get_dashboard_stats()
        tg_stats = self.get_runtime(uid, "telegram").db.get_dashboard_stats()
        x_stats = self.get_runtime(uid, "x").db.get_dashboard_stats()
        return {
            "web": {
                "total_sources": web_stats["total_sites"],
                "active_sources": web_stats["active_sites"],
                "total_posts": web_stats["total_posts"],
                "db_size_bytes": web_stats["db_size_bytes"],
                "latest_post_utc": web_stats["latest_post_utc"],
            },
            "telegram": {
                "total_sources": tg_stats["total_channels"],
                "active_sources": tg_stats["active_channels"],
                "total_posts": tg_stats["total_posts"],
                "db_size_bytes": tg_stats["db_size_bytes"],
                "latest_post_utc": tg_stats["latest_post_utc"],
            },
            "x": {
                "total_sources": x_stats["total_channels"],
                "active_sources": x_stats["active_channels"],
                "total_posts": x_stats["total_posts"],
                "db_size_bytes": x_stats["db_size_bytes"],
                "latest_post_utc": x_stats["latest_post_utc"],
            },
            "totals": {
                "sources": (
                    web_stats["total_sites"]
                    + tg_stats["total_channels"]
                    + x_stats["total_channels"]
                ),
                "posts": (
                    web_stats["total_posts"]
                    + tg_stats["total_posts"]
                    + x_stats["total_posts"]
                ),
            },
        }
