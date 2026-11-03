import sqlite3
import io
from openpyxl import load_workbook
import pytest
import app
import collector
import worker
from test_app import client, post

INN='7707083893'
HTML='''<html><meta charset="utf-8"><h1>Тестовый бренд</h1><p>ИНН 7707083893</p><p>+7 (474) 200-00-00</p><a href="mailto:sales@example.org">Почта</a><a href="/contacts">Контакты</a><script type="application/ld+json">{"@type":"Organization","taxID":"7707083893","legalName":"Тестовая организация","name":"Тестовый бренд","description":"Производство мебели","address":{"addressLocality":"Липецк","streetAddress":"Тестовая улица"}}</script></html>'''.encode()

def add_source(client):
    return post(client,'/collector/sources',{'name':'Каталог','url':'https://example.org/','max_pages':'3','interval_hours':'24'})

def test_checksums_and_ambiguous_pages():
    assert collector.valid_inn(INN)
    assert collector.valid_inn('500100732259')
    for invalid in ['7707083894','0000000000','4800000000','１２３４５６７８９０']:
        assert not collector.valid_inn(invalid)
    parsed,_=collector.parse_page(HTML,'https://example.org/')
    assert parsed['name']=='Тестовая организация'
    assert parsed['brand']=='Тестовый бренд'
    assert parsed['address']=='Липецк, Тестовая улица'
    assert parsed['email']=='sales@example.org'
    assert collector.parse_page(HTML+b'<p>7736050003</p>','https://example.org/')[0] is None

def test_collection_review_and_preservation(client):
    add_source(client)
    visited=[]
    def fetch(url):
        visited.append(url)
        return HTML+b'<a href="https://other.example.org">Other</a>',url
    with app.db() as c: source=c.execute('SELECT * FROM collection_sources').fetchone()
    result=collector.collect(source,fetch,pause=lambda _:None)
    assert len(visited)==2
    assert all(url.startswith('https://example.org/') for url in visited)
    assert 'ошибок: 0' in result
    with app.db() as c:
        assert c.execute('SELECT count(*) FROM companies').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM collection_candidates').fetchone()[0]==1
    response=client.get('/collector')
    assert response.status_code==200 and 'Тестовая организация' in response.text
    post(client,'/collector/candidates/1',{'action':'approve'})
    with app.db() as c:
        row=c.execute('SELECT * FROM companies').fetchone()
        assert row['inn']==INN and row['consent']==0
        assert row['description']=='Производство мебели'
        c.execute("UPDATE companies SET brand='Проверенный бренд' WHERE id=?",(row['id'],))
    exported=load_workbook(io.BytesIO(client.get('/export').data))
    assert exported.active['A2'].value=='Тестовая организация'
    assert exported.active['G2'].value=='sales@example.org'
    collector.collect(source,fetch,pause=lambda _:None)
    with app.db() as c:
        assert c.execute('SELECT status FROM collection_candidates').fetchone()[0]=='approved'
        assert c.execute('SELECT brand FROM companies').fetchone()[0]=='Проверенный бренд'

def test_scheduler_dedup_and_pause(client,monkeypatch):
    add_source(client)
    worker.schedule_sources(); worker.schedule_sources()
    post(client,'/collector/sources/1',{'action':'run'})
    with app.db() as c: assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    import collector as module
    original=module.collect
    monkeypatch.setattr(module,'collect',lambda source,fetcher:original(source,fetcher,pause=lambda _:None))
    monkeypatch.setattr(worker,'fetch',lambda url:(HTML,url))
    assert worker.process_one()
    worker.schedule_sources()
    with app.db() as c:
        assert c.execute('SELECT status FROM jobs').fetchone()[0]=='done'
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    post(client,'/collector/sources/1',{'action':'toggle'})
    with app.db() as c: c.execute('DELETE FROM jobs')
    worker.schedule_sources()
    with app.db() as c: assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0

def test_failed_source_status(client,monkeypatch):
    add_source(client)
    worker.schedule_sources()
    def fail(url): raise TimeoutError('Sensitive upstream response')
    monkeypatch.setattr(worker,'fetch',fail)
    import collector as module
    original=module.collect
    monkeypatch.setattr(module,'collect',lambda source,fetcher:original(source,fetcher,pause=lambda _:None))
    assert worker.process_one()
    with app.db() as c:
        job=c.execute('SELECT * FROM jobs').fetchone()
        assert job['status']=='failed' and job['error']=='ValueError'
        assert 'ошибок: 1' in c.execute('SELECT last_result FROM collection_sources').fetchone()[0]

def test_existing_data_not_overwritten(client):
    add_source(client)
    post(client,'/companies',{'name':'Оператор','inn':INN,'brand':'Проверено'})
    with app.db() as c: source=c.execute('SELECT * FROM collection_sources').fetchone()
    collector.collect(source,lambda url:(HTML,url),pause=lambda _:None)
    post(client,'/collector/candidates/1',{'action':'approve'})
    with app.db() as c:
        row=c.execute('SELECT * FROM companies').fetchone()
        assert row['name']=='Оператор' and row['brand']=='Проверено'
        assert row['email']=='sales@example.org'

def test_upgrade_existing_database(tmp_path,monkeypatch):
    path=tmp_path/'legacy.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,company_id INTEGER,offer_id INTEGER,status TEXT, error TEXT,created_at TEXT,finished_at TEXT)')
    monkeypatch.setenv('DATABASE_PATH',str(path))
    app.init_db(); app.init_db()
    with app.db() as c: assert 'source_id' in {r['name'] for r in c.execute('PRAGMA table_info(jobs)')}

def test_search_discovers_sites_and_outputs_candidates(client,monkeypatch):
    post(client,'/collector/search',{'queries':'Липецк заводы ИНН\nЕлец ИП ИНН','max_pages':'5'})
    worker.schedule_sources()
    rss=b'<rss version="2.0"><channel><item><link>https://example.org/</link></item><item><link>https://example.org/</link></item><item><link>file:///etc/passwd</link></item></channel></rss>'
    visited=[]
    def fetch(url):
        visited.append(url)
        return (rss if url.startswith('https://www.bing.com/') else HTML),url
    monkeypatch.setattr(worker,'fetch',fetch)
    original=collector.collect
    monkeypatch.setattr(collector,'collect',lambda source,fetcher:original(source,fetcher,pause=lambda _:None))
    assert worker.process_one()
    with app.db() as c:
        assert c.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM collection_candidates').fetchone()[0]==1
        assert c.execute('SELECT status FROM jobs ORDER BY id').fetchone()[0]=='done'
        assert 'Результатов поиска: 1' in c.execute('SELECT last_result FROM collection_sources ORDER BY id').fetchone()[0]
    assert visited[0].startswith('https://www.bing.com/search?')
    assert 'https://example.org/' in visited
    assert 'Начать поиск в интернете' in client.get('/collector').text

def test_search_unavailable_is_reported(client,monkeypatch):
    post(client,'/collector/search',{'queries':'Липецк ИНН','max_pages':'5'})
    worker.schedule_sources()
    monkeypatch.setattr(worker,'fetch',lambda url:(b'<html>captcha</html>',url))
    assert worker.process_one()
    with app.db() as c:
        assert c.execute('SELECT status FROM jobs').fetchone()[0]=='failed'
        assert 'Поиск недоступен' in c.execute('SELECT last_result FROM collection_sources').fetchone()[0]
        assert c.execute('SELECT count(*) FROM collection_candidates').fetchone()[0]==0


def test_concurrent_initialization(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'concurrent.db'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:app.init_db(),range(4)))
    with app.db() as c:
        assert 'kind' in {r['name'] for r in c.execute('PRAGMA table_info(collection_sources)')}
        assert 'source_id' in {r['name'] for r in c.execute('PRAGMA table_info(jobs)')}
