import functools
import hmac
import io
import os
import secrets
import sqlite3
from pathlib import Path
from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).parent
FIELDS = {'Организация':'name', 'ИНН':'inn', 'Бренд':'brand', 'Описание чем занимаются':'description', 'Адрес':'address', 'Телефон':'phone', 'Почта':'email', 'Сайт':'website'}

def db():
    con = sqlite3.connect(os.environ.get('DATABASE_PATH', str(ROOT / 'data/app.db')), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS companies (
          id INTEGER PRIMARY KEY, inn TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          brand TEXT DEFAULT '', description TEXT DEFAULT '', address TEXT DEFAULT '',
          phone TEXT DEFAULT '', email TEXT DEFAULT '', website TEXT DEFAULT '',
          consent INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
          source TEXT DEFAULT '', checked_at TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS offers (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
          subject TEXT NOT NULL, body TEXT NOT NULL, attachment TEXT);
        CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
          company_id INTEGER REFERENCES companies(id), offer_id INTEGER REFERENCES offers(id),
          status TEXT DEFAULT 'pending', error TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          finished_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS unique_delivery ON jobs(company_id, offer_id) WHERE kind='send';
        ''')

def upsert(c, values, source):
    inn = str(values.get('inn', '')).strip()
    if not inn.isdigit() or len(inn) not in (10,12) or not str(values.get('name','')).strip():
        raise ValueError('Нужны организация и ИНН из 10 или 12 цифр')
    fields = ['inn','name','brand','description','address','phone','email','website']
    clean = {key: str(values.get(key) or '').strip() for key in fields}
    c.execute('INSERT INTO companies ('+','.join(fields)+',source) VALUES ('+','.join('?' for _ in range(9))+') ON CONFLICT(inn) DO UPDATE SET '+','.join(f"{k}=CASE WHEN excluded.{k} != '' THEN excluded.{k} ELSE companies.{k} END" for k in fields if k!='inn')+',source=excluded.source,updated_at=CURRENT_TIMESTAMP', [clean[k] for k in fields]+[source])

def import_excel(content, source):
    book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows = book.active.iter_rows(values_only=True)
    headers = next(rows, ())
    mapping = {i:FIELDS[h] for i,h in enumerate(headers) if h in FIELDS}
    if not {'inn','name'} <= set(mapping.values()):
        raise ValueError('В Excel обязательны колонки Организация и ИНН')
    count = 0
    with db() as c:
        for row in rows:
            if not any(v is not None for v in row):
                continue
            values = {key: row[i] for i,key in mapping.items() if i < len(row)}
            upsert(c, values, source)
            count += 1
    book.close()
    return count

def create_app():
    app = Flask(__name__)
    # Persisted outside source control; no hardcoded session secret.
    secret_path = ROOT / 'data/session.key'
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32)); secret_path.chmod(0o600)
    app.secret_key = os.environ.get('SESSION_SECRET') or secret_path.read_text()
    app.config.update(MAX_CONTENT_LENGTH=12*1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
    init_db()

    @app.before_request
    def protect():
        password = os.environ.get('APP_PASSWORD')
        if password:
            auth = request.authorization
            if not auth or not hmac.compare_digest(auth.password or '', password) or auth.username != 'admin':
                return 'Требуется авторизация', 401, {'WWW-Authenticate':'Basic realm="BMSeller"'}
        elif request.remote_addr not in ('127.0.0.1','::1'):
            abort(503, 'Задайте APP_PASSWORD перед внешним доступом')
        if request.method == 'POST' and (not session.get('csrf') or not hmac.compare_digest(session['csrf'], request.form.get('csrf',''))):
            abort(400, 'Обновите страницу и повторите действие')
        session.setdefault('csrf', secrets.token_hex(24))

    @app.get('/')
    def index():
        q = request.args.get('q','')
        with db() as c:
            companies = c.execute('SELECT * FROM companies WHERE name LIKE ? OR brand LIKE ? OR inn LIKE ? OR description LIKE ? ORDER BY updated_at DESC,id DESC LIMIT 500', ['%'+q+'%']*4).fetchall()
            stats = c.execute("SELECT count(*) total, sum(email!='') emails, sum(consent=1 AND excluded=0) eligible FROM companies").fetchone()
            offers = c.execute('SELECT * FROM offers ORDER BY id DESC').fetchall()
            jobs = c.execute('SELECT jobs.*, companies.name FROM jobs LEFT JOIN companies ON companies.id=jobs.company_id ORDER BY jobs.id DESC LIMIT 30').fetchall()
        return render_template('index.html', companies=companies, stats=stats, offers=offers,jobs=jobs,q=q,fields=FIELDS)

    @app.post('/companies')
    def save_company():
        try:
            with db() as c:
                upsert(c, request.form, 'Ручной ввод')
            flash('Компания сохранена')
        except ValueError as e:
            flash(str(e))
        return redirect('/')

    @app.post('/companies/<int:cid>/permissions')
    def permissions(cid):
        with db() as c:
            c.execute('UPDATE companies SET consent=?,excluded=? WHERE id=?', (int('consent' in request.form),int('excluded' in request.form),cid))
        flash('Настройки контакта обновлены')
        return redirect('/')

    @app.post('/import')
    def upload():
        f = request.files.get('file')
        try:
            if not f: raise ValueError('Выберите Excel')
            count = import_excel(f.read(), 'Импорт Excel')
            flash(f'Обработано строк: {count}')
        except Exception as e:
            app.logger.warning('Import failed: %s', type(e).__name__)
            flash('Импорт отменён. Проверьте формат XLSX, заголовки, названия и ИНН.')
        return redirect('/')

    @app.get('/export')
    def export():
        book = Workbook(); sheet = book.active; sheet.title = 'Компании'
        sheet.append(list(FIELDS))
        with db() as c:
            for row in c.execute('SELECT * FROM companies ORDER BY id'):
                # Force strings so external content cannot become spreadsheet formulas.
                sheet.append([str(row[f] or '') for f in FIELDS.values()])
                for cell in sheet[sheet.max_row]: cell.data_type = 's'
        buf=io.BytesIO(); book.save(buf); buf.seek(0)
        return send_file(buf,as_attachment=True,download_name='companies.xlsx')

    @app.post('/offers')
    def offer():
        title,subject,body = [request.form.get(k,'').strip() for k in ('title','subject','body')]
        if not all((title,subject,body)) or '\n' in subject or '\r' in subject: abort(400)
        attachment=None
        f=request.files.get('presentation')
        if f and f.filename:
            if not f.filename.lower().endswith('.pdf'): abort(400,'Презентация должна быть PDF')
            content=f.read()
            if not content.startswith(b'%PDF-'): abort(400,'Неверный формат PDF')
            attachment=str(ROOT/'data'/f'{secrets.token_hex(16)}.pdf')
            Path(attachment).write_bytes(content)
        with db() as c:
            c.execute('INSERT INTO offers(title,subject,body,attachment) VALUES (?,?,?,?)',(title,subject,body,attachment))
        flash('Коммерческое предложение сохранено')
        return redirect('/')

    @app.post('/queue')
    def queue():
        kind=request.form.get('kind')
        if kind not in ('enrich','send'): abort(400)
        ids=request.form.getlist('company')
        offer_id=request.form.get('offer_id') if kind=='send' else None
        count=0
        with db() as c:
            if kind=='send' and not c.execute('SELECT id FROM offers WHERE id=?',(offer_id,)).fetchone(): abort(400,'Выберите КП')
            for cid in ids:
                row=c.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
                if not row: continue
                if kind=='send' and (not row['consent'] or row['excluded'] or not row['email']): continue
                if kind=='enrich' and not row['website']: continue
                count += c.execute('INSERT OR IGNORE INTO jobs(kind,company_id,offer_id) VALUES (?,?,?)',(kind,cid,offer_id)).rowcount
        flash(f'Добавлено заданий: {count}. Отправка доступна только с согласием, без исключения и с почтой. Повторы одного КП пропускаются.')
        return redirect('/')
    return app

if __name__=='__main__':
    create_app().run(host=os.environ.get('HOST','127.0.0.1'), port=int(os.environ.get('PORT','8000')),debug=False)
