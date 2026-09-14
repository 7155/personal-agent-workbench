"""Bounded human-readable review of immutable project drafts."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .errors import TeamError


def draft_diff(team: Any, space_id: str, draft_id: str, actor: str) -> dict[str, object]:
    draft = team.workspaces.read_draft(space_id, draft_id, actor)
    project = team.workspaces.ensure_project(space_id, target_branch=draft['targetBranch'])
    repository = Path(str(project['repositoryPath']))
    # This repository and both object IDs belong to the trusted publisher.
    # Disable external diff/textconv even if a published tree has attributes.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            completed = subprocess.run(
                ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                 'diff', '--no-ext-diff', '--no-textconv', '--no-color', str(draft['baseCommit']), str(draft['draftCommit']), '--'],
                cwd=repository, stdout=output, stderr=error, check=False,
                env=team.workspaces._git_env(), timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamError(503, 'draft_diff_unavailable', 'The fixed draft diff is temporarily unavailable') from exc
        if completed.returncode:
            raise TeamError(503, 'draft_diff_unavailable', 'The fixed draft diff could not be read')
        output.seek(0)
        content = output.read(512 * 1024 + 1)
    return {'draftId': draft_id, 'baseCommit': draft['baseCommit'], 'draftCommit': draft['draftCommit'],
            'diff': content[:512 * 1024].decode('utf-8', errors='replace'), 'truncated': len(content) > 512 * 1024}


__all__ = ['draft_diff']
