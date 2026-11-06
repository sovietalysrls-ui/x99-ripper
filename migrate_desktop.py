#!/usr/bin/env python3
"""Move app-owned MKV jobs from the stopped desktop queue to the web queue."""
import json
import os
from pathlib import Path

import ripper


def save_json(path, data):
    tmp = path.with_suffix('.migrate-tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def migrate(desktop_path, web_path, root):
    root = Path(root).resolve()
    desktop = json.loads(Path(desktop_path).read_text())
    state = json.loads(Path(web_path).read_text())
    queue = state.setdefault('queue', [])
    existing = {job.get('source') for job in queue}
    moved = []
    for job in desktop:
        if job.get('status') not in ('in coda', 'compressione', 'interrotto', 'errore'):
            continue
        source = Path(job.get('source') or '/nonexistent')
        folder = Path(job.get('folder') or '/nonexistent')
        if (not source.is_file() or source.is_symlink() or
                source.resolve().parent != folder.resolve() or
                not source.resolve().is_relative_to(root) or str(source) in existing):
            continue
        output = folder / (job['name'] + '_H265.mkv')
        if output.exists():
            continue
        ripper.probe(source)
        archived = ripper.archive_partial(output)
        queue.append({'name': job['name'], 'folder': str(folder),
                      'source': str(source), 'title_id': job.get('title_id'),
                      'status': 'pronto', 'previous_partials': [str(archived)] if archived else []})
        job['status'] = 'trasferito'
        job['error'] = 'MKV conservato e trasferito alla coda web.'
        existing.add(str(source))
        moved.append(job['name'])
    if moved:
        state.update(phase='In attesa', detail=f'{len(moved)} film pronti per la compressione GPU.',
                     job=None, queue=queue)
        save_json(web_path, state)
        save_json(desktop_path, desktop)
    return moved


if __name__ == '__main__':
    if os.environ.get('X99_RIPPER_OUTPUT_DIR') is None:
        raise SystemExit('Imposta X99_RIPPER_OUTPUT_DIR sulla cartella dei film.')
    moved = migrate(ripper.STATE / 'jobs.json', ripper.STATE / 'web-state.json', ripper.ROOT)
    print('Trasferiti:', ', '.join(moved) if moved else 'nessuno')
