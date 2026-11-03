import ssl
import pytest
import worker

@pytest.mark.parametrize('security,port',[('ssl',465),('starttls',587)])
def test_smtp_tls_and_presentation(monkeypatch,tmp_path,security,port):
    for key,value in {'SMTP_HOST':'smtp.mail.ru','SMTP_USER':'test@mail.ru','SMTP_PASSWORD':'test-only','SMTP_FROM':'test@mail.ru','SMTP_SECURITY':security}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.delenv('SMTP_PORT',raising=False)
    presentation=tmp_path/'presentation.pdf'; presentation.write_bytes(b'%PDF-1.4 test')
    events=[]
    class SMTP:
        def __init__(self,host,actual_port,timeout,**kwargs):
            assert host=='smtp.mail.ru' and actual_port==port
            if security=='ssl':
                assert kwargs['context'].verify_mode==ssl.CERT_REQUIRED
                assert kwargs['context'].check_hostname
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def starttls(self,context):
            assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
            events.append('tls')
        def login(self,user,password):
            assert user=='test@mail.ru' and password=='test-only'
            events.append('login')
        def send_message(self,message):
            assert message['From']=='test@mail.ru'
            assert 'Тестовая компания' in message.get_body().get_content()
            assert len(list(message.iter_attachments()))==1
            events.append('send')
    monkeypatch.setattr(worker.smtplib,'SMTP_SSL',SMTP)
    monkeypatch.setattr(worker.smtplib,'SMTP',SMTP)
    worker.send({'consent':1,'excluded':0,'email':'recipient@example.org','name':'Тестовая компания','brand':'Бренд'},
                {'subject':'КП','body':'Здравствуйте, {{organization}}','attachment':str(presentation)})
    assert events==(['login','send'] if security=='ssl' else ['tls','login','send'])

def test_insecure_mode_rejected(monkeypatch):
    for key in ('SMTP_HOST','SMTP_USER','SMTP_PASSWORD','SMTP_FROM'): monkeypatch.setenv(key,'test')
    monkeypatch.setenv('SMTP_SECURITY','plain')
    with pytest.raises(ValueError,match='SMTP_SECURITY'):
        worker.send({'consent':1,'excluded':0,'email':'recipient@example.org','name':'Тест','brand':''},
                    {'subject':'КП','body':'Текст','attachment':None})
