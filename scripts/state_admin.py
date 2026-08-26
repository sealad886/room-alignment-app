from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_alignment.state_admin import backup, dry_run_migration, main, restore, validate_database


__all__ = ["backup", "dry_run_migration", "restore", "validate_database"]


if __name__ == "__main__":
    raise SystemExit(main())
