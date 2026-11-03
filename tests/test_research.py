import io
import pytest
from openpyxl import Workbook
import app
import research
import worker
from test_app import client,post
from test_collector import HTML,INN


def registry(rows):
    b=Workbook(); b.active.append(['Организация','ИНН','Код региона','Статус','Адрес'])
    for row in rows: b.active.append(row)
    f=io.BytesIO(); b.save(f); return f.getvalue()


def seed():
    research.import_registry(registry([['Компания',INN,'48','действует','Липецк']]),'fixture')
    with app.db() as c: return c.execute('SELECT * FROM companies').fetchone()


def test_registry_filter_and_atomic_import(client):
    content=registry([['Компания',INN,'48','действует','Липецк'],['Другая','7736050003','77','active','Москва']])
    assert research.import_registry(content,'test')==(1,1)
    assert research.import_registry(content,'test')==(1,1)
    with app.db() as c:
        assert c.execute('SELECT count(*) FROM companies').fetchone()[0]==1
        assert c.execute('SELECT enabled FROM research_state').fetchone()[0]==0
    with pytest.raises(ValueError): research.import_registry(registry([['Новая','500100732259','48','active',''],['Ошибка','123','48','active','']]),'bad')
    with app.db() as c: assert c.execute('SELECT count(*) FROM companies').fetchone()[0]==1


def test_research_match_review_and_provenance(client):
    company=seed()
    calls=[]
    def search(query): calls.append(query); return ['https://example.org/']
    assert research.research_company(company,lambda url:(HTML,url),search,pause=lambda _:None)==1
    assert len(calls)==1 and INN in calls[0]
    with app.db() as c:
        assert c.execute('SELECT stage FROM research_state').fetchone()[0]=='review'
        assert c.execute('SELECT email FROM companies').fetchone()[0]==''
        assert c.execute("SELECT strength FROM research_evidence WHERE field='website'").fetchone()[0]=='inn_match'
    post(client,'/collector/candidates/1',{'action':'approve'})
    with app.db() as c:
        row=c.execute('SELECT * FROM companies').fetchone()
        assert row['email']=='sales@example.org' and row['consent']==0
        assert c.execute('SELECT stage FROM research_state').fetchone()[0]=='complete'
    assert client.get('/collector').status_code==200
    assert client.get('/registry/template').status_code==200


def test_mismatched_inn_and_directories_not_accepted(client):
    company=seed()
    wrong=HTML.replace(INN.encode(),b'7736050003')
    visited=[]
    def fetch(url): visited.append(url); return wrong,url
    assert research.research_company(company,fetch,lambda q:['https://rusprofile.ru/id/1','https://example.org/'],pause=lambda _:None)==0
    assert all('rusprofile' not in url for url in visited)
    with app.db() as c:
        assert c.execute('SELECT count(*) FROM research_evidence').fetchone()[0]==0
        assert c.execute('SELECT stage FROM research_state').fetchone()[0]=='not_found'


def test_missing_key_no_jobs_and_retry_backoff(client,monkeypatch):
    company=seed()
    post(client,'/research/control',{'company':[str(company['id'])],'action':'start'})
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY',raising=False)
    research.schedule_research()
    with app.db() as c:
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
        assert c.execute('SELECT stage FROM research_state').fetchone()[0]=='blocked'
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','test-only')
    research.schedule_research(); research.schedule_research()
    def fail(*args): raise TimeoutError('sensitive')
    monkeypatch.setattr(research,'research_company',fail)
    assert worker.process_one()
    with app.db() as c:
        r=c.execute('SELECT * FROM research_state').fetchone()
        assert r['stage']=='error' and r['attempts']==1 and r['next_run_at']
        assert 'sensitive' not in r['last_error']
    research.schedule_research()
    with app.db() as c: assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def test_registry_inactive_disables_research(client):
    seed()
    research.import_registry(registry([['Компания',INN,'48','ликвидирована','']]),'update')
    with app.db() as c: assert c.execute('SELECT stage FROM research_state').fetchone()[0]=='registry_excluded'
    assert research.import_registry(registry([['Компания',INN,'48','active','']]),'update')==(1,0)
    with app.db() as c: assert c.execute('SELECT stage FROM research_state').fetchone()[0]=='registry'

def test_registry_feed_from_source(client,monkeypatch):
    post(client,'/collector/sources',{'name':'Реестр','url':'https://example.org/feed.xlsx','kind':'registry','max_pages':'1','interval_hours':'24'})
    payload=registry([['Компания',INN,'48','active','Липецк']])
    monkeypatch.setattr(worker,'fetch',lambda url:(payload,url))
    worker.schedule_sources()
    assert worker.process_one()
    with app.db() as c:
        assert c.execute('SELECT count(*) FROM registry_entries').fetchone()[0]==1
        assert 'Принято: 1' in c.execute('SELECT last_result FROM collection_sources').fetchone()[0]
    assert 'XLSX-реестр' in client.get('/collector').text


def test_brave_request_does_not_forward_key_to_websites(monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','test-only')
    class Response:
        status_code=200
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def raise_for_status(self): pass
        def iter_content(self,size): yield b'{"web":{"results":[{"url":"https://example.org/"}]}}'
    def get(url,**kwargs):
        assert url=='https://api.search.brave.com/res/v1/web/search'
        assert kwargs['headers']['X-Subscription-Token']=='test-only'
        assert kwargs['allow_redirects'] is False
        return Response()
    monkeypatch.setattr(research.requests,'get',get)
    assert research.brave_search('query')==['https://example.org/']

def test_multiple_inns_on_site_never_mix_contacts(client):
    company=seed()
    def fetch(url):
        return (HTML if url.endswith('/') else HTML+b'<p>7736050003 other company</p>'),url
    assert research.research_company(company,fetch,lambda q:['https://example.org/'],pause=lambda _:None)==0
    with app.db() as c: assert c.execute('SELECT count(*) FROM research_evidence').fetchone()[0]==0
