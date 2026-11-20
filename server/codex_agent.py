"""Codex planning adapter; credentials remain managed by the local CLI."""
import shutil
from .intelligence import create_edit


def codex_path():
    return shutil.which('codex.exe') or shutil.which('codex')


async def codex_edit(project,prompt,folder):
    return await create_edit(project,prompt,folder,{'provider':'codex'})
