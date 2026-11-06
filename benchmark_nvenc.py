#!/usr/bin/env python3
"""Compare 60-second CPU and NVENC samples without touching source media."""
import argparse
import json
import subprocess
import time
from pathlib import Path

import ripper


def main():
    parser = argparse.ArgumentParser(description='Benchmark H.265 CPU e Quadro P1000')
    parser.add_argument('source', type=Path, help='MKV sorgente già acquisito')
    parser.add_argument('output_dir', type=Path, help='Cartella nuova per i risultati')
    parser.add_argument('--start', type=int, default=0, help='Secondo di partenza del campione')
    parser.add_argument('--gpu-quality', type=int, default=28, help='Qualità NVENC (predefinita: 28)')
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error('Sorgente MKV assente')
    if args.start < 0 or not 0 <= args.gpu_quality <= 51:
        parser.error('Partenza o qualità fuori intervallo')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    results = []
    for label, encoder, preset in (('cpu', 'x265_10bit', 'slow'),
                                   ('nvenc', 'nvenc_h265_10bit', 'medium')):
        target = args.output_dir / (label + '.mkv')
        quality = 22 if label == 'cpu' else args.gpu_quality
        cmd = ripper.HB + ['-i', str(args.source), '-o', str(target), '-f', 'av_mkv',
                           '--start-at', f'seconds:{args.start}', '--stop-at', 'seconds:60',
                           '-e', encoder, '--encoder-preset', preset, '-q', str(quality),
                           '-a', 'none', '-s', 'none']
        started = time.monotonic()
        with (args.output_dir / (label + '.log')).open('w') as log:
            completed = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        result = {'encoder': encoder, 'quality': quality, 'start_seconds': args.start,
                  'seconds': round(time.monotonic() - started, 1),
                  'exit_code': completed.returncode,
                  'size_bytes': target.stat().st_size if target.exists() else None}
        if completed.returncode == 0:
            try:
                result['probe'] = ripper.probe(target)
            except Exception as exc:
                result['probe_error'] = str(exc)
        results.append(result)
        if completed.returncode:
            break
    (args.output_dir / 'results.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    raise SystemExit(0 if len(results) == 2 and all(r['exit_code'] == 0 for r in results) else 1)


if __name__ == '__main__':
    main()
