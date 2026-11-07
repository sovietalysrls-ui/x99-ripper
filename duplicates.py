"""Conservative local duplicate checks; weak disc names never block acquisition."""
import difflib
import hashlib
import re
import unicodedata
from pathlib import Path

import film_library


GENERIC = {'dvdvolume', 'dvd', 'disc', 'disk', 'bluray', 'bdmv', 'video_ts', 'unknown'}


def normalized(title):
    value = unicodedata.normalize('NFKD', str(title or '')).casefold()
    value = ''.join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', value)
    value = re.sub(r'\b(?:disc|disk|disco|dvd|bluray|blu ray|scn|h265)\b', ' ', value)
    return re.sub(r'[^a-z0-9]+', '', value)


def sample_hash(path):
    """Fingerprint the source, including size and three spaced samples."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open('rb') as source:
        for position in (0, max(0, size // 2 - 524288), max(0, size - 1048576)):
            source.seek(position)
            digest.update(source.read(1048576))
    return digest.hexdigest()


def candidates(title, duration_seconds, paths, *, year=None, wikidata_id='', imdb_id='', source=None, source_lookup=None):
    if year is None:
        found_year = re.search(r'\((19\d{2}|20\d{2})\)\s*$', str(title or ''))
        year = int(found_year.group(1)) if found_year else None
    key = normalized(title)
    weak_name = key in GENERIC or len(key) < 3
    found = []
    for path in paths:
        info = film_library.read(path)
        known_title = info.get('title') or path.stem.removesuffix('_H265').replace('_', ' ')
        known_key = normalized(known_title)
        identity = (bool(wikidata_id and wikidata_id == info.get('wikidata_id')) or
                    bool(imdb_id and imdb_id == info.get('imdb_id')))
        title_match = not weak_name and (key == known_key or
                      len(key) >= 9 and difflib.SequenceMatcher(None, key, known_key).ratio() >= .92)
        if year and info.get('year') and abs(int(year) - int(info['year'])) > 1:
            title_match = False
        exact_source = False
        if source:
            source_name = info.get('source_filename', '')
            candidate_source = path.parent / source_name if source_name and Path(source_name).name == source_name else None
            if (not candidate_source or not candidate_source.is_file()) and source_lookup:
                known = source_lookup.get(str(path))
                candidate_source = Path(known) if known else None
            if candidate_source and candidate_source.is_file() and not candidate_source.is_symlink():
                exact_source = source.stat().st_size == candidate_source.stat().st_size and sample_hash(source) == sample_hash(candidate_source)
        if not (identity or title_match or exact_source):
            continue
        known_duration = info.get('duration_seconds')
        if known_duration and duration_seconds and not exact_source and not identity:
            if abs(float(known_duration) - float(duration_seconds)) > max(300, float(known_duration) * .05):
                continue
        found.append({'path': str(path), 'title': known_title, 'year': info.get('year'),
                      'reason': 'stesso contenuto' if exact_source else 'stesso identificativo' if identity else 'titolo simile',
                      'duration_seconds': known_duration})
    return found
