from pathlib import Path
from unittest.mock import patch
import uuid

def install_recycle_fixture(case, root):
    def recycle(path):
        path=Path(path)
        if not path.exists(): return None
        destination=Path(root)/'test-trash'/uuid.uuid4().hex/path.name
        destination.parent.mkdir(parents=True,exist_ok=True)
        path.rename(destination)
        return str(destination)
    for target in ['mixcut.server.move_to_trash','mixcut.trash.move_to_trash']:
        mock=patch(target,side_effect=recycle)
        mock.start();case.addCleanup(mock.stop)
