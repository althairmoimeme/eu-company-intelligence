"""JSON-based checkpoints for scraper resumability."""
import json
import os
from datetime import datetime


def checkpoint_path(scraper_name: str, checkpoint_dir: str = "checkpoints") -> str:
    os.makedirs(checkpoint_dir, exist_ok=True)
    return os.path.join(checkpoint_dir, f"{scraper_name}.json")


def load_checkpoint(scraper_name: str, checkpoint_dir: str = "checkpoints") -> dict:
    path = checkpoint_path(scraper_name, checkpoint_dir)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_checkpoint(scraper_name: str, data: dict, checkpoint_dir: str = "checkpoints"):
    path = checkpoint_path(scraper_name, checkpoint_dir)
    data["updated_at"] = datetime.utcnow().isoformat()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def clear_checkpoint(scraper_name: str, checkpoint_dir: str = "checkpoints"):
    path = checkpoint_path(scraper_name, checkpoint_dir)
    if os.path.exists(path):
        os.remove(path)
