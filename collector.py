"""Bounded, review-first collection from operator-selected public websites."""
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit, urlunsplit, urlencode
from bs4 import BeautifulSoup
from app import db

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
INN = re.compile(r'(?<!\d)(?:\d{12}|\d{10})(?!\d)')
PHONE = re.compile(r'(?:\+7|8)[\s(.-]*\d{3}[\s).−-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)')


def valid_inn(value):
    if not value.isascii() or not value.isdigit() or len(value) not in (10, 12) or len(set(value)) == 1:
        return False
    digits = list(map(int, value))
    def check(weights):
        return sum(a*b for a,b in zip(digits, weights)) % 11 % 10
    if len(value) == 10:
        return check([2,4,10,3,5,9,4,6,8]) == digits[9]
    return (check([7,2,4,10,3,5,9,4,6,8]) == digits[10]
            and check([3,7,2,4,10,3,5,9,4,6,8]) == digits[11])


def canonical(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Нужен публичный HTTP(S) URL без логина и пароля')
    if parts.port not in (None,80,443):
        raise ValueError('Поддерживаются только стандартные HTTP(S) порты')
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', parts.query, ''))


def structured_objects(soup):
    def walk(node):
        if isinstance(node, list):
            for item in node: yield from walk(item)
        elif isinstance(node, dict):
            yield node
            for value in node.values():
                if isinstance(value, (dict,list)): yield from walk(value)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue


def scalar(value):
    return str(value).strip() if isinstance(value, (str,int)) else ''


def address_text(value):
    if isinstance(value, dict):
        return ', '.join(scalar(value.get(k)) for k in ('postalCode','addressRegion','addressLocality','streetAddress') if scalar(value.get(k)))
    return scalar(value)


def parse_page(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    objects = list(structured_objects(soup))
    for el in soup(['script','style','noscript']): el.decompose()
    text = soup.get_text(' ', strip=True)
    # Directory pages containing many companies cannot establish whose email is whose.
    inns = {value for value in INN.findall(text) if valid_inn(value)}
    for item in objects:
        value = scalar(item.get('taxID'))
        if valid_inn(value): inns.add(value)
    if len(inns) != 1:
        return None, soup
    inn = next(iter(inns))
    item = next((o for o in objects if scalar(o.get('taxID')) == inn), {})
    # A title is a candidate brand, never proof of legal ownership.
    h1 = soup.find('h1')
    heading = h1.get_text(' ',strip=True) if h1 else (soup.title.get_text(' ',strip=True) if soup.title else '')
    name = scalar(item.get('legalName'))
    if not name:
        legal = re.search(r'(?:ООО|АО|ПАО|ОАО|ЗАО|ИП)\s+[«"„][^»"“]{2,150}[»"“]', text)
        name = legal.group(0) if legal else scalar(item.get('name')) or heading
    if not name: return None, soup
    emails = set(EMAIL.findall(text))
    phones = set(PHONE.findall(text))
    for link in soup.select('a[href]'):
        href = link['href']
        if href.lower().startswith('mailto:'): emails.update(EMAIL.findall(href[7:].split('?')[0]))
        if href.lower().startswith('tel:'): phones.add(href[4:].strip())
    emails.update(EMAIL.findall(scalar(item.get('email'))))
    if scalar(item.get('telephone')): phones.add(scalar(item['telephone']))
    meta = soup.find('meta',attrs={'name': re.compile('^description$',re.I)})
    description = scalar(item.get('description')) or (meta.get('content','') if meta else '')
    address = address_text(item.get('address'))
    if not address:
        el = soup.find(attrs={'itemprop':'address'})
        address = el.get_text(' ',strip=True) if el else ''
    brand = scalar(item.get('brand'))
    if isinstance(item.get('brand'),dict): brand = scalar(item['brand'].get('name'))
    brand = brand or scalar(item.get('name')) or heading
    website = url
    if scalar(item.get('url')):
        try: website = canonical(urljoin(url, scalar(item['url'])))
        except ValueError: pass
    return dict(inn=inn,name=name[:250],brand=brand[:250],description=description[:2000],
                address=address[:1000],email='; '.join(sorted(emails)),phone='; '.join(sorted(phones)),
                website=website,source=url), soup


def search_url(query):
    return 'https://www.bing.com/search?' + urlencode({'format':'rss','q':query,'setlang':'ru-RU','cc':'ru'})


def search_links(content):
    try:
        root=ET.fromstring(content)
    except ET.ParseError:
        raise ValueError('Поиск не вернул RSS: возможны ограничение доступа или CAPTCHA') from None
    if root.tag != 'rss': raise ValueError('Неверный формат ответа поиска')
    links=[]
    for item in root.findall('./channel/item'):
        try: url=canonical(item.findtext('link',''))
        except ValueError: continue
        if url not in links: links.append(url)
    return links[:20]


def collect(source, fetcher, pause=time.sleep):
    """Walk same-origin pages only; fetched external links are never followed."""
    root = canonical(source['url'])
    is_search = source['kind'] == 'search'
    search_count = None
    if is_search:
        try:
            content, _ = fetcher(root)
            seeds = search_links(content)
            search_count = len(seeds)
        except Exception:
            with db() as c:
                c.execute("UPDATE collection_sources SET last_run_at=CURRENT_TIMESTAMP,last_result=? WHERE id=?",('Поиск недоступен. Проверьте доступ к www.bing.com и формат RSS-ответа.',source['id']))
            raise
    else:
        seeds = [root]
    queue = [(url,0,urlsplit(url).netloc) for url in seeds]
    seen=set(); found=0; errors=0
    limit = source['max_pages']
    while queue and len(seen) < limit:
        url,depth,origin = queue.pop(0)
        if url in seen: continue
        seen.add(url)
        try:
            content, final = fetcher(url)
            if urlsplit(final).netloc != origin:
                errors += 1
                continue
            candidate,soup = parse_page(content,final)
            if candidate:
                with db() as c:
                    keys = ['inn','name','brand','description','address','email','phone','website','source']
                    c.execute('INSERT INTO collection_candidates(source_id,'+','.join(keys)+') VALUES ('+','.join('?' for _ in range(10))+') ON CONFLICT(source_id,inn) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in keys if k!='inn')+",found_at=CURRENT_TIMESTAMP WHERE collection_candidates.status='pending'", [source['id']]+[candidate[k] for k in keys])
                found += 1
            if depth < 2:
                for a in soup.select('a[href]'):
                    try: target=canonical(urljoin(final,a['href']))
                    except ValueError: continue
                    if urlsplit(target).netloc != origin or target in seen: continue
                    if re.search(r'\.(?:pdf|zip|xlsx?|png|jpe?g|gif|svg|css|js)(?:\?|$)', target,re.I): continue
                    if len(queue) < limit*5: queue.append((target,depth+1,origin))
        except Exception:
            errors += 1
        finally:
            pause(2)
    prefix = f'Результатов поиска: {search_count}; ' if is_search else ''
    result = prefix + f'Страниц: {len(seen)}; карточек с ИНН: {found}; ошибок: {errors}'
    with db() as c:
        c.execute('UPDATE collection_sources SET last_run_at=CURRENT_TIMESTAMP,last_result=? WHERE id=?',(result,source['id']))
    if seen and errors == len(seen): raise ValueError('Ни одна страница источника не загружена')
    return result
