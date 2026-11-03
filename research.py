"""Registry-first research. No paid calls until research is enabled by the operator."""
import io
import os
import re
import time
from urllib.parse import urlsplit, urljoin
from openpyxl import load_workbook
import requests
from bs4 import BeautifulSoup
from app import db, upsert, FIELDS
from collector import canonical, valid_inn, parse_page, EMAIL, PHONE, INN

class MissingSearchCredentials(ValueError): pass


def import_registry(content, source):
    book=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
    try:
        rows=book.active.iter_rows(values_only=True)
        headers=next(rows,())
        required=['Организация','ИНН','Код региона','Статус']
        if not all(k in headers for k in required): raise ValueError('Нужны колонки: '+', '.join(required))
        count=skipped=0
        with db() as c:
            for values in rows:
                row=dict(zip(headers,values))
                if not any(v is not None for v in values): continue
                inn=str(row.get('ИНН') or '').strip()
                if not valid_inn(inn) or not str(row.get('Организация') or '').strip(): raise ValueError('Некорректный ИНН или название')
                if str(row.get('Код региона')).strip() not in ('48','48.0') or str(row.get('Статус') or '').strip().lower() not in ('действует','active'):
                    # Keep an existing company, but stop research if the latest registry says inactive/out of region.
                    c.execute("UPDATE registry_entries SET status=?,region=?,imported_at=CURRENT_TIMESTAMP WHERE company_id IN (SELECT id FROM companies WHERE inn=?)",(str(row.get('Статус') or ''),str(row.get('Код региона') or ''),inn))
                    c.execute('UPDATE research_state SET enabled=0,stage=\'registry_excluded\' WHERE company_id IN (SELECT id FROM companies WHERE inn=?)',(inn,))
                    skipped+=1; continue
                existing=c.execute('SELECT * FROM companies WHERE inn=?',(inn,)).fetchone()
                data={'inn':inn,'name':str(row['Организация']), 'address':str(row.get('Адрес') or ''),'description':str(row.get('Описание чем занимаются') or '')}
                if existing: data={key:existing[key] or data.get(key,'') for key in FIELDS.values()}
                upsert(c,data,source)
                cid=c.execute('SELECT id FROM companies WHERE inn=?',(inn,)).fetchone()[0]
                c.execute("INSERT INTO registry_entries(company_id,region,status,source) VALUES (?,'48','active',?) ON CONFLICT(company_id) DO UPDATE SET region='48',status='active',source=excluded.source,imported_at=CURRENT_TIMESTAMP",(cid,source))
                c.execute('INSERT OR IGNORE INTO research_state(company_id) VALUES (?)',(cid,))
                c.execute("UPDATE research_state SET stage='registry',next_run_at=NULL WHERE company_id=? AND stage='registry_excluded'",(cid,))
                count+=1
        return count,skipped
    finally: book.close()


def brave_search(query):
    token=os.environ.get('BRAVE_SEARCH_API_KEY')
    if not token: raise MissingSearchCredentials('Не настроен Brave Search API')
    # Fixed trusted API endpoint; credentials are never sent to discovered websites.
    with requests.get('https://api.search.brave.com/res/v1/web/search',params={'q':query,'count':5,'search_lang':'ru'},headers={'Accept':'application/json','X-Subscription-Token':token},timeout=(10,30),allow_redirects=False,stream=True) as response:
        response.raise_for_status()
        if response.status_code != 200: raise ValueError('Неожиданный ответ поискового API')
        content=bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content)>2*1024*1024: raise ValueError('Слишком большой ответ поиска')
        import json
        data=json.loads(content)
    if not isinstance(data,dict) or ('web' in data and not isinstance(data['web'],dict)): raise ValueError('Неверный формат поиска')
    links=[]
    for item in data.get('web',{}).get('results',[]):
        try: url=canonical(item['url'])
        except (KeyError,TypeError,ValueError): continue
        if url not in links: links.append(url)
    return links[:5]


DIRECTORIES=('rusprofile.ru','list-org.com','checko.ru','zachestnyibiznes.ru','2gis.ru','yandex.ru','sbis.ru')

def research_company(company, fetcher, searcher=brave_search, pause=time.sleep):
    queries=[f'"{company["inn"]}" официальный сайт контакты',f'{company["name"]} {company["address"]} ИНН {company["inn"]}']
    candidates=[]; seen=set(); failures=0; loaded=0
    for query in queries:
        for seed in searcher(query):
            try: seed=canonical(seed)
            except ValueError: continue
            host=urlsplit(seed).hostname
            if any(host==d or host.endswith('.'+d) for d in DIRECTORIES): continue
            queue=[seed]; pages=[]; conflicting_ids=False
            while queue and len(pages)<3 and len(seen)<15:
                url=queue.pop(0)
                if url in seen: continue
                seen.add(url)
                try:
                    content,final=fetcher(url)
                    if urlsplit(final).netloc!=urlsplit(seed).netloc: continue
                    loaded+=1
                    if any(valid_inn(v) and v!=company['inn'] for v in INN.findall(content.decode('utf-8',errors='ignore'))):
                        conflicting_ids=True
                    parsed,soup=parse_page(content,final)
                    pages.append((parsed,soup,final))
                    for a in soup.select('a[href]'):
                        target=urljoin(final,a['href'])
                        if any(word in (a.get_text()+' '+a['href']).lower() for word in ('контакт','реквиз','contact','requisit')):
                            try: target=canonical(target)
                            except ValueError: continue
                            if urlsplit(target).netloc==urlsplit(seed).netloc and target not in seen and len(queue)<4: queue.append(target)
                except Exception: failures+=1
                finally: pause(2)
            matched=[p for p in pages if p[0] and p[0]['inn']==company['inn']]
            conflicting=any(p[0] and p[0]['inn']!=company['inn'] for p in pages)
            if not matched or conflicting or conflicting_ids: continue
            base=dict(matched[0][0]); base['name']=company['name']; base['website']=seed
            evidence=[('website',seed,matched[0][2],'inn_match')]
            # Only use contacts from pages on the domain whose INN was matched.
            emails=set(); phones=set()
            for _,soup,url in pages:
                text=soup.get_text(' ',strip=True)
                for field,pattern,values in [('email',EMAIL,emails),('phone',PHONE,phones)]:
                    extracted=set(pattern.findall(text))
                    for a in soup.select('a[href]'):
                        href=a['href']
                        if field=='email' and href.lower().startswith('mailto:'): extracted.update(EMAIL.findall(href[7:]))
                        if field=='phone' and href.lower().startswith('tel:'): extracted.add(href[4:].strip())
                    values.update(extracted)
                    evidence.extend((field,v,url,'same_domain') for v in extracted)
            base['email']='; '.join(sorted(emails)); base['phone']='; '.join(sorted(phones))
            for field in ('brand','description','address'):
                if base[field]: evidence.append((field,base[field],base['source'],'proposed'))
            candidates.append((base,evidence))
        if candidates: break
    if not loaded and failures: raise ConnectionError('Сайты недоступны')
    with db() as c:
        if c.execute("SELECT 1 FROM research_state WHERE company_id=? AND stage='registry_excluded'",(company['id'],)).fetchone():
            return 0
        for values,evidence in candidates:
            source_url='research:'+str(company['id'])+':'+urlsplit(values['website']).netloc
            c.execute("INSERT OR IGNORE INTO collection_sources(name,url,enabled,kind) VALUES (?,?,0,'research')",('Поиск по ИНН '+company['inn'],source_url))
            sid=c.execute('SELECT id FROM collection_sources WHERE url=?',(source_url,)).fetchone()[0]
            keys=list(FIELDS.values())+['source']
            old=c.execute('SELECT * FROM collection_candidates WHERE source_id=? AND inn=?',(sid,company['inn'])).fetchone()
            if not old or any(old[k]!=values[k] for k in keys):
                c.execute('INSERT INTO collection_candidates(source_id,'+','.join(keys)+') VALUES ('+','.join('?' for _ in range(len(keys)+1))+') ON CONFLICT(source_id,inn) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in keys)+",status='pending',found_at=CURRENT_TIMESTAMP",[sid]+[values[k] for k in keys])
            for field,value,url,strength in evidence:
                c.execute('INSERT INTO research_evidence(company_id,field,value,url,strength) VALUES (?,?,?,?,?) ON CONFLICT(company_id,field,value,url) DO UPDATE SET checked_at=CURRENT_TIMESTAMP',(company['id'],field,value,url,strength))
        stage='not_found'
        if candidates:
            statuses={r[0] for r in c.execute("SELECT status FROM collection_candidates cc JOIN collection_sources cs ON cs.id=cc.source_id WHERE cc.inn=? AND cs.kind='research'",(company['inn'],))}
            stage='review' if 'pending' in statuses else ('complete' if 'approved' in statuses else 'not_found')
        c.execute("UPDATE research_state SET stage=?,attempts=0,last_error='',checked_at=CURRENT_TIMESTAMP,next_run_at=datetime('now',?) WHERE company_id=?",(stage,'+30 days' if candidates else '+7 days',company['id']))
    return len(candidates)


def schedule_research():
    with db() as c:
        if not os.environ.get('BRAVE_SEARCH_API_KEY'):
            c.execute("UPDATE research_state SET stage='blocked',last_error='Нужен BRAVE_SEARCH_API_KEY' WHERE enabled=1 AND stage!='registry_excluded'")
            return
        c.execute("""INSERT INTO jobs(kind,company_id) SELECT 'research',r.company_id FROM research_state r
          WHERE enabled=1 AND stage!='registry_excluded' AND EXISTS(SELECT 1 FROM registry_entries e WHERE e.company_id=r.company_id AND e.status='active') AND (next_run_at IS NULL OR next_run_at<=CURRENT_TIMESTAMP)
          AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.company_id=r.company_id AND j.kind='research' AND j.status IN ('pending','running'))
          ORDER BY r.checked_at,r.company_id LIMIT 10""")
