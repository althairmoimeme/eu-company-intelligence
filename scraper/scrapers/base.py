"""Abstract base scraper."""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator
from ..pipeline.normalizer import CompanyRecord
from ..utils.checkpoints import load_checkpoint, save_checkpoint

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    name: str = "base"
    country: str = "XX"

    def __init__(self, config: dict, checkpoint_dir: str = "checkpoints"):
        self.config = config
        self.checkpoint_dir = checkpoint_dir
        self._checkpoint: dict = {}

    def load_checkpoint(self) -> dict:
        self._checkpoint = load_checkpoint(self.name, self.checkpoint_dir)
        return self._checkpoint

    def save_checkpoint(self, data: dict):
        self._checkpoint.update(data)
        save_checkpoint(self.name, self._checkpoint, self.checkpoint_dir)

    @abstractmethod
    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        """Yield CompanyRecord objects."""
        ...

    async def _sleep(self, seconds: float):
        await asyncio.sleep(seconds)
