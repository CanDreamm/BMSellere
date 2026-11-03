"""Run separately: python worker.py. Network destinations must be public."""
import ipaddress
import os
import re
import smtplib
import socket
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from app import db, init_db, import_excel


def fetch(url):
    # Do not follow redirects to unchecked destinations or accept private addresses.
    for _ in range(4):
        parsed=urlparse(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,80,443):
            raise ValueError('Нужен публичный HTTP(S) адрес')
        addresses=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80))
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Локальные и служебные адреса запрещены')
        with requests.get(url,timeout=(10,20),allow_redirects=False,stream=True,headers={'User-Agent':'BMSellerContactCollector/0.1'}) as response:
            if response.status_code in (301,302,303,307,308):
                url=urljoin(url,response.headers['Location']); continue
            response.raise_for_status()
            result=bytearray()
            for chunk in response.iter_content(65536):
                result.extend(chunk)
                if len(result)>12*1024*1024: raise ValueError('Ответ слишком большой')
            return bytes(result),url
    raise ValueError('Слишком много перенаправлений')


def enrich(company):
    content,url=fetch(company['website'])
    soup=BeautifulSoup(content,'html.parser')
    contacts=[]
    for a in soup.select('a[href]'):
        href=a['href']
        candidate=urljoin(url,href)
        if urlparse(candidate).netloc==urlparse(url).netloc and any(x in (href+' '+a.get_text()).lower() for x in ('contact','контакт')):
            contacts.append(candidate)
    pages=[soup]
    for contact in list(dict.fromkeys(contacts))[:2]:
        payload,_=fetch(contact); pages.append(BeautifulSoup(payload,'html.parser'))
    emails=set(); phones=set()
    for page in pages:
        for a in page.select('a[href]'):
            href=a['href']
            if href.lower().startswith('mailto:'):
                emails.add(href[7:].split('?')[0].strip())
            if href.lower().startswith('tel:'): phones.add(href[4:].strip())
        emails.update(re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',page.get_text(' ')))
    # Contact data are candidates. Brand ownership is not inferred from a page title.
    with db() as c:
        c.execute("UPDATE companies SET email=CASE WHEN email='' THEN ? ELSE email END,phone=CASE WHEN phone='' THEN ? ELSE phone END,source=?,checked_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",('; '.join(sorted(emails)),'; '.join(sorted(phones)),url,company['id']))


def send(company,offer):
    if not company['consent'] or company['excluded']: raise ValueError('Нет согласия или контакт исключён')
    required=['SMTP_HOST','SMTP_USER','SMTP_PASSWORD','SMTP_FROM']
    if not all(os.environ.get(k) for k in required): raise ValueError('Не настроен SMTP')
    addresses=[x.strip() for x in re.split('[;,]',company['email']) if x.strip()]
    if not addresses or any(not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',x) for x in addresses): raise ValueError('Некорректная почта')
    message=EmailMessage()
    message['Subject']=offer['subject']; message['From']=os.environ['SMTP_FROM']; message['To']=', '.join(addresses)
    message.set_content(offer['body'].replace('{{organization}}',company['name']).replace('{{brand}}',company['brand']))
    if offer['attachment']:
        message.add_attachment(Path(offer['attachment']).read_bytes(),maintype='application',subtype='pdf',filename='presentation.pdf')
    with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ.get('SMTP_PORT','587')),timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context()); smtp.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD']); smtp.send_message(message)


def process_one():
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        job=c.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if not job: return False
        c.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
    try:
        with db() as c:
            company=c.execute('SELECT * FROM companies WHERE id=?',(job['company_id'],)).fetchone()
            offer=c.execute('SELECT * FROM offers WHERE id=?',(job['offer_id'],)).fetchone()
        if job['kind']=='enrich': enrich(company)
        else: send(company,offer)
        status,error='done',''
    except Exception as exc:
        # Store safe error class, never SMTP credentials or upstream response bodies.
        status,error='failed',type(exc).__name__
    with db() as c:
        c.execute('UPDATE jobs SET status=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?',(status,error,job['id']))
    return True


def main():
    init_db(); last_sync=0; last_enrich=0
    while True:
        now=time.time()
        if os.environ.get('COMPANY_FEED_URL') and now-last_sync>int(os.environ.get('FEED_INTERVAL_SECONDS','86400')):
            try:
                payload,url=fetch(os.environ['COMPANY_FEED_URL']); count=import_excel(payload,url)
                print(f'Feed imported: {count}',flush=True)
            except Exception as exc: print(f'Feed failed: {type(exc).__name__}',flush=True)
            last_sync=now
        if now-last_enrich>3600:
            with db() as c:
                c.execute("""INSERT INTO jobs(kind,company_id) SELECT 'enrich',id FROM companies c
                WHERE website!='' AND (checked_at IS NULL OR checked_at < datetime('now','-30 days'))
                AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.company_id=c.id AND j.kind='enrich'
                AND (j.status IN ('pending','running') OR j.created_at>datetime('now','-1 day'))) LIMIT 100""")
            last_enrich=now
        if not process_one(): time.sleep(5)
        else: time.sleep(float(os.environ.get('JOB_DELAY_SECONDS','2')))

if __name__=='__main__': main()
