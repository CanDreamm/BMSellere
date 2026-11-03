import io
import pytest
from openpyxl import Workbook, load_workbook
import app
import worker

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'test.db'))
    monkeypatch.delenv('APP_PASSWORD',raising=False)
    application=app.create_app(); application.config['TESTING']=True
    return application.test_client()

def post(client,path,data):
    client.get('/')
    with client.session_transaction() as s: data['csrf']=s['csrf']
    return client.post(path,data=data)

def test_import_export_and_rollback(client):
    wb=Workbook(); ws=wb.active
    ws.append(['Организация','ИНН','Бренд','Почта'])
    ws.append(['Тест','4800000000','=1+1','info@example.org'])
    ws['C2'].data_type='s'
    buf=io.BytesIO(); wb.save(buf)
    assert app.import_excel(buf.getvalue(),'test')==1
    assert app.import_excel(buf.getvalue(),'test')==1
    response=client.get('/export')
    exported=load_workbook(io.BytesIO(response.data))
    assert exported.active.max_row==2
    assert exported.active['C2'].data_type=='s'
    ws.append(['Плохая строка','123','',''])
    buf=io.BytesIO(); wb.save(buf)
    with pytest.raises(ValueError): app.import_excel(buf.getvalue(),'bad')
    with app.db() as c: assert c.execute('SELECT count(*) FROM companies').fetchone()[0]==1

def test_consent_queue_and_duplicate_prevention(client):
    post(client,'/companies',{'name':'Тест','inn':'4800000000','email':'info@example.org'})
    post(client,'/offers',{'title':'КП','subject':'Тест','body':'Здравствуйте, {{organization}}'})
    post(client,'/queue',{'kind':'send','company':['1'],'offer_id':'1'})
    with app.db() as c: assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    post(client,'/companies/1/permissions',{'consent':'on'})
    for _ in range(2): post(client,'/queue',{'kind':'send','company':['1'],'offer_id':'1'})
    with app.db() as c: assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1

def test_csrf_and_remote_guard(client):
    assert client.post('/companies',data={}).status_code==400
    assert client.get('/',environ_base={'REMOTE_ADDR':'192.0.2.1'}).status_code==503

def test_worker_extracts_contacts(client,monkeypatch):
    post(client,'/companies',{'name':'Тест','inn':'4800000000','website':'https://example.org'})
    monkeypatch.setattr(worker,'fetch',lambda url:(b'<a href="mailto:sales@example.org">Email</a><a href="tel:+74742000000">Phone</a>',url))
    post(client,'/queue',{'kind':'enrich','company':['1']})
    assert worker.process_one()
    with app.db() as c:
        company=c.execute('SELECT * FROM companies').fetchone()
        assert company['email']=='sales@example.org'
        assert company['phone']=='+74742000000'
        assert company['consent']==0
        assert c.execute('SELECT status FROM jobs').fetchone()[0]=='done'

def test_private_fetch_blocked():
    with pytest.raises(ValueError): worker.fetch('http://127.0.0.1')

def test_send_rechecks_exclusion(client,monkeypatch):
    post(client,'/companies',{'name':'Тест','inn':'4800000000','email':'info@example.org'})
    post(client,'/offers',{'title':'КП','subject':'Тест','body':'Текст'})
    post(client,'/companies/1/permissions',{'consent':'on'})
    post(client,'/queue',{'kind':'send','company':['1'],'offer_id':'1'})
    post(client,'/companies/1/permissions',{'consent':'on','excluded':'on'})
    assert worker.process_one()
    with app.db() as c: assert c.execute('SELECT status FROM jobs').fetchone()[0]=='failed'
