"""Local film catalog and optional, user-reviewed Wikidata lookup."""
import json
import difflib
import re
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

API = 'https://www.wikidata.org/w/api.php'
AGENT = 'DiscFlow/0.10 (personal DVD catalog; https://github.com/sovietalysrls-ui/x99-ripper)'
QID = re.compile(r'^Q[1-9][0-9]{0,11}$')


def request(**params):
    url = API + '?' + urllib.parse.urlencode({'format': 'json', **params})
    req = urllib.request.Request(url, headers={'User-Agent': AGENT})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def search(query):
    query = query.strip()
    if not 2 <= len(query) <= 100:
        raise ValueError('Scrivi almeno due lettere del titolo.')
    data = request(action='wbsearchentities', search=query, language='it', type='item', limit=8)
    return [{'id': item['id'], 'title': item.get('label', ''),
             'description': item.get('description', '')} for item in data.get('search', [])
            if QID.fullmatch(item.get('id', ''))]


def _values(entity, prop):
    for claim in entity.get('claims', {}).get(prop, []):
        if claim.get('rank') == 'deprecated':
            continue
        value = claim.get('mainsnak', {}).get('datavalue', {}).get('value')
        if value is not None:
            yield value


def _label(entity):
    labels = entity.get('labels', {})
    return (labels.get('it') or labels.get('en') or {}).get('value', '')


def details(qid):
    if not QID.fullmatch(qid):
        raise ValueError('Identificativo non valido.')
    entity = request(action='wbgetentities', ids=qid, props='labels|claims|sitelinks', languages='it|en')['entities'][qid]
    def ids(prop, maximum):
        return [v['id'] for v in _values(entity, prop) if isinstance(v, dict) and QID.fullmatch(v.get('id', ''))][:maximum]
    people = ids('P57', 4)
    cast = ids('P161', 12)
    genres = ids('P136', 8)
    original_languages = ids('P364', 3)
    wanted = list(dict.fromkeys(people + cast + genres + original_languages))
    entities = request(action='wbgetentities', ids='|'.join(wanted), props='labels|claims', languages='it|en').get('entities', {}) if wanted else {}
    names = lambda keys: [_label(entities.get(key, {})) for key in keys if _label(entities.get(key, {}))]
    language_codes = [code for qid in original_languages for code in _values(entities.get(qid, {}), 'P220')
                      if isinstance(code, str) and re.fullmatch(r'[a-z]{3}', code)]
    years = []
    for value in _values(entity, 'P577'):
        if isinstance(value, dict) and value.get('precision', 0) >= 9:
            match = re.match(r'^[+-](\d{4})-', value.get('time', ''))
            if match:
                years.append(int(match.group(1)))
    imdb = next((v for v in _values(entity, 'P345') if isinstance(v, str) and re.fullmatch(r'tt\d+', v)), '')
    wiki_title = entity.get('sitelinks', {}).get('enwiki', {}).get('title', '')
    original_title = entity.get('labels', {}).get('en', {}).get('value', '') or next(
        (v['text'] for v in _values(entity, 'P1476')
         if isinstance(v, dict) and v.get('language') == 'en' and v.get('text')), wiki_title)
    cover_url = ''
    if wiki_title:
        url = 'https://en.wikipedia.org/api/rest_v1/page/summary/' + urllib.parse.quote(wiki_title.replace(' ', '_'))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': AGENT}), timeout=10) as response:
                summary = json.load(response)
            image = summary.get('thumbnail', {}).get('source', '')
            if image.startswith('https://upload.wikimedia.org/'):
                cover_url = image
        except (OSError, ValueError):
            pass
    runtimes = []
    for value in _values(entity, 'P2047'):
        if isinstance(value, dict) and value.get('unit', '').endswith('/Q7727'):
            try: runtimes.append(float(value['amount']))
            except (TypeError, ValueError): pass
    return {'title': _label(entity), 'year': min(years) if years else None,
            'original_title': original_title,
            'director': names(people), 'cast': names(cast), 'genres': names(genres),
            'original_language': language_codes[0] if language_codes else '',
            'runtime_minutes': min(runtimes) if runtimes else None,
            'wikidata_id': qid, 'imdb_id': imdb, 'cover_url': cover_url,
            'cover_source': 'https://en.wikipedia.org/wiki/' + urllib.parse.quote(wiki_title.replace(' ', '_')) if cover_url else '',
            'source': 'Wikidata / Wikipedia'}


def _movie_query(filename):
    text = Path(filename).stem.removesuffix('_H265').replace('_', ' ').strip()
    text = re.sub(r'\b(?:SCN|DVD|DISC|DISCO)\b$', '', text, flags=re.I).strip()
    return re.sub(r'\bNY$', 'New York', text, flags=re.I)


def _wikipedia_match(query):
    url = 'https://en.wikipedia.org/w/api.php?' + urllib.parse.urlencode({
        'action': 'query', 'list': 'search', 'srsearch': query + ' film',
        'srnamespace': 0, 'srlimit': 5, 'format': 'json'})
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': AGENT}), timeout=12) as response:
        pages = json.load(response).get('query', {}).get('search', [])
    if not pages:
        return []
    ids = '|'.join(str(p['pageid']) for p in pages[:5])
    url = 'https://en.wikipedia.org/w/api.php?' + urllib.parse.urlencode({
        'action': 'query', 'pageids': ids, 'prop': 'pageprops', 'ppprop': 'wikibase_item', 'format': 'json'})
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': AGENT}), timeout=12) as response:
        found = json.load(response).get('query', {}).get('pages', {})
    return [p.get('pageprops', {}).get('wikibase_item', '') for p in found.values()]


def suggest(path, title_hint=None):
    """Rank film candidates from the disc name and runtime; never guess from the title alone."""
    query = _movie_query(title_hint or path.name)
    if len(query) < 2:
        return []
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                           capture_output=True, text=True, timeout=20)
    try: minutes = float(probe.stdout.strip()) / 60
    except ValueError: minutes = None
    candidates = [x['id'] for x in search(query) if 'film' in x['description'].lower() or 'movie' in x['description'].lower()]
    if not candidates:
        candidates = [x for x in _wikipedia_match(query) if QID.fullmatch(x)]
    result = []
    for qid in dict.fromkeys(candidates[:6]):
        try: movie = details(qid)
        except (OSError, KeyError, ValueError): continue
        if not movie['title'] or not movie['year']:
            continue
        title_score = max(difflib.SequenceMatcher(None, query.casefold(), name.casefold()).ratio()
                          for name in (movie['title'], movie.get('original_title') or movie['title']))
        runtime = movie.get('runtime_minutes')
        time_score = max(0, 1 - abs(minutes - runtime) / 25) if minutes and runtime else 0
        score = round(0.65 * title_score + 0.35 * time_score, 3)
        result.append({**movie, 'confidence': score})
    return sorted(result, key=lambda x: x['confidence'], reverse=True)


def automatic_choice(path, title_hint=None):
    matches = suggest(path, title_hint)
    if not matches:
        return None
    best = matches[0]
    runner_up = matches[1]['confidence'] if len(matches) > 1 else 0
    return best if best['confidence'] >= 0.78 and best['confidence'] - runner_up >= 0.12 else None


def sidecar(path):
    return path.parent / 'discflow-film.json'


def read(path):
    try:
        return json.loads(sidecar(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def safe_title(title):
    title = title.strip()
    if not title or len(title) > 120 or any(ord(c) < 32 or c in '/\\<>|?*' for c in title):
        raise ValueError('Titolo non valido per un nome di file.')
    return title.rstrip('. ')


def save(path, metadata):
    """Apply reviewed data to the final filename and Jellyfin NFO without overwrites."""
    title = safe_title(metadata['title'])
    year = metadata.get('year')
    if year is not None and not 1888 <= int(year) <= 2100:
        raise ValueError('Anno non valido.')
    filename_title = re.sub(r'\s*:\s*', ' - ', title)
    stem = filename_title + (f' ({year})' if year else '')
    target = path.with_name(stem + '_H265.mkv')
    if target != path and target.exists():
        raise FileExistsError('Esiste già un film con questo titolo. Nessun file è stato cambiato.')
    data = {'title': title, 'year': int(year) if year else None,
            'director': list(metadata.get('director') or [])[:4],
            'cast': list(metadata.get('cast') or [])[:12],
            'genres': list(metadata.get('genres') or [])[:8],
            'wikidata_id': metadata.get('wikidata_id', ''),
            'imdb_id': metadata.get('imdb_id', ''),
            'original_language': metadata.get('original_language', ''),
            'cover_url': metadata.get('cover_url', ''), 'cover_source': metadata.get('cover_source', ''),
            'source': metadata.get('source', 'manuale')}
    nfo = path.parent / 'movie.nfo'
    if nfo.exists():
        try:
            root = ET.parse(nfo).getroot()
            if root.findtext('discflow') != '1':
                raise FileExistsError('movie.nfo esiste già: verifica i metadati prima di modificarli.')
        except ET.ParseError as exc:
            raise ValueError('movie.nfo non è leggibile.') from exc
    movie = ET.Element('movie')
    ET.SubElement(movie, 'discflow').text = '1'
    ET.SubElement(movie, 'title').text = title
    if data['year']:
        ET.SubElement(movie, 'year').text = str(data['year'])
    for director in data['director']:
        ET.SubElement(movie, 'director').text = director
    for genre in data['genres']:
        ET.SubElement(movie, 'genre').text = genre
    for actor in data['cast']:
        ET.SubElement(ET.SubElement(movie, 'actor'), 'name').text = actor
    if data['imdb_id']:
        ET.SubElement(movie, 'imdbid').text = data['imdb_id']
    if data['wikidata_id']:
        ET.SubElement(movie, 'uniqueid', type='wikidata').text = data['wikidata_id']
    if data['cover_url']:
        ET.SubElement(movie, 'thumb').text = data['cover_url']
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    json_tmp = sidecar(path).with_suffix('.json.tmp')
    nfo_tmp = nfo.with_suffix('.nfo.tmp')
    json_tmp.write_bytes(payload)
    ET.ElementTree(movie).write(nfo_tmp, encoding='utf-8', xml_declaration=True)
    if target != path:
        path.rename(target)
    json_tmp.replace(sidecar(target))
    nfo_tmp.replace(nfo)
    return target
