import os
import json
import time
import hmac
import random
import hashlib
import secrets
import sqlite3
import base64
import io
from pathlib import Path
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager, contextmanager
from collections import Counter
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image

BASE = Path(__file__).resolve().parent
CONTENT_DIR = BASE / 'content'
DATA_DIR = BASE / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE / '.env')
DEV_MODE = os.getenv('DEV_MODE', 'false').lower() == 'true'
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
FREE_SECONDS = 20
PUZZLE_COOLDOWN = 15
QUOTES_COOLDOWN = 15

# Рецепты крафта: 
# Формат: 'Название результата': { 'из': 'Название исходной редкости', 'сколько': Кол-во }
CRAFT_RECIPES = {
    'Необычная': {'from': 'Обычная', 'amount': 3},
    'Редкая': {'from': 'Необычная', 'amount': 3},
    'Эпическая': {'from': 'Редкая', 'amount': 3},
    'Легендарная': {'from': 'Эпическая', 'amount': 2},
    'Мифическая': {'from': 'Легендарная', 'amount': 3}, # Изменили 5 на 3
}

POINTS = {
    'Обычная': 100, 'Необычная': 250, 'Редкая': 500,
    'Эпическая': 750, 'Легендарная': 1000, 'Мифическая': 2500,
}

DROP_WEIGHTS = {
    'Обычная': 500, 'Необычная': 250, 'Редкая': 150,
    'Эпическая': 75, 'Легендарная': 15, 'Мифическая': 10,
}
DB = DATA_DIR / 'game.db'

@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def initialize():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, last_free REAL NOT NULL DEFAULT 0)')
        try:
            c.execute('ALTER TABLE users ADD COLUMN photo_url TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE users ADD COLUMN username TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass

        c.execute('CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY, anime TEXT NOT NULL, name TEXT NOT NULL, rarity TEXT NOT NULL, image TEXT NOT NULL)')
        
        # ДОБАВЛЯЕМ КОЛОНКУ ОПИСАНИЯ В БАЗУ
        try:
            c.execute('ALTER TABLE cards ADD COLUMN description TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass

        c.execute('CREATE TABLE IF NOT EXISTS inventory(user_id INTEGER REFERENCES users(id), card_id INTEGER REFERENCES cards(id), count INTEGER NOT NULL, PRIMARY KEY(user_id,card_id))')
        c.execute('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), kind TEXT NOT NULL, state TEXT NOT NULL, deadline REAL NOT NULL, done INTEGER NOT NULL DEFAULT 0, started_at REAL)')
        
        try:
            c.execute('ALTER TABLE sessions ADD COLUMN started_at REAL')
        except sqlite3.OperationalError:
            pass

        # --- ТАБЛИЦА ДЛЯ ОБМЕНОВ ---
        c.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER REFERENCES users(id),
                receiver_id INTEGER REFERENCES users(id),
                sender_cards TEXT NOT NULL,
                receiver_cards TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL
            )
        ''')
        # ---------------------------

        # Читаем все JSON-файлы из папки content/cards/
        cards_dir = CONTENT_DIR / 'cards'
        cards_dir.mkdir(parents=True, exist_ok=True)
        
        all_cards = []
        card_id = 1
        
        # Проходим по всем файлам в алфавитном порядке
        for file_path in sorted(cards_dir.glob('*.json')):
            default_anime_name = file_path.stem.capitalize()
            try:
                cards_data = json.loads(file_path.read_text(encoding='utf-8'))
                for card in cards_data:
                    if card['rarity'] not in POINTS:
                        raise ValueError(f"Неизвестная редкость: {card['rarity']} в файле {file_path.name}")
                    
                    # Если в первой карте файла есть поле "anime_title", берем его. Иначе берем имя файла.
                    final_anime_name = card.get('anime_title', default_anime_name)
                    
                    all_cards.append({
                        'id': card_id,
                        'anime': final_anime_name,  # Используем красивое название
                        'name': card['name'],
                        'rarity': card['rarity'],
                        'image': card.get('image', ''),
                        'description': card.get('description', '')
                    })
                    card_id += 1
            except Exception as e:
                print(f"Ошибка чтения {file_path.name}: {e}")

        # Сохраняем в базу
        for card in all_cards:
            c.execute(
                'INSERT INTO cards (id, anime, name, rarity, image, description) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET anime=excluded.anime, name=excluded.name, rarity=excluded.rarity, image=excluded.image, description=excluded.description',
                (card['id'], card['anime'], card['name'], card['rarity'], card['image'], card['description'])
            )

@asynccontextmanager
async def lifespan(app):
    if not DEV_MODE and not BOT_TOKEN:
        raise RuntimeError('Set BOT_TOKEN in .env or enable DEV_MODE for localhost only')
    initialize()
    yield

app = FastAPI(lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE/'static'), name='static')

@app.get('/')
def index():
    return FileResponse(BASE/'static'/'index.html')

def identity(authorization):
    if authorization == 'dev' and DEV_MODE:
        return {'id': 1, 'first_name': 'Тестовый игрок', 'username': 'testuser'}
    if not authorization or not authorization.startswith('tma '):
        raise HTTPException(401, 'Открой приложение через Telegram')
    try:
        pairs = parse_qsl(authorization[4:], keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError()
        received = data.pop('hash')
        check = '\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        age = time.time() - int(data['auth_date'])
        if not hmac.compare_digest(received, expected) or not -30 <= age <= 86400:
            raise ValueError()
        user = json.loads(data['user'])
        if not isinstance(user.get('id'), int) or isinstance(user['id'], bool):
            raise ValueError()
        
        if 'photo_url' not in user: user['photo_url'] = ''
        if 'username' not in user: user['username'] = ''
            
        return user
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, 'Вход недействителен или устарел. Переоткрой приложение.')

def player(c, authorization):
    user = identity(authorization)
    name = str(user.get('first_name') or 'Игрок')[:100]
    photo = user.get('photo_url', '')
    username = str(user.get('username') or '')[:100]
    c.execute('INSERT INTO users(id,name,photo_url,username) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, photo_url=excluded.photo_url, username=excluded.username', (user['id'],name,photo,username))
    return user['id']

def card_dict(row):
    result = dict(row)
    result['points'] = POINTS[result['rarity']]
    return result

def reward(c, uid):
    rows = c.execute('SELECT rarity, COUNT(*) AS amount FROM cards GROUP BY rarity').fetchall()
    counts = {r['rarity']: r['amount'] for r in rows}
    pool = [(r, w) for r, w in DROP_WEIGHTS.items() if w > 0 and counts.get(r, 0) > 0]
    if not pool:
        raise HTTPException(503, 'Каталог карт пуст')
    rarities, weights = zip(*pool)
    rarity = random.choices(rarities, weights=weights, k=1)[0]
    card = c.execute('SELECT * FROM cards WHERE rarity=? ORDER BY RANDOM() LIMIT 1', (rarity,)).fetchone()
    c.execute('INSERT INTO inventory(user_id, card_id, count) VALUES(?, ?, 1) ON CONFLICT(user_id, card_id) DO UPDATE SET count = count + 1', (uid, card['id']))
    return card_dict(card)

@app.get('/api/me')
def me(authorization: str | None = Header(default=None)):
    with db() as c:
        tg_user = identity(authorization)
        uid = tg_user['id']
        
        name = str(tg_user.get('first_name') or tg_user.get('username') or 'Игрок')[:100]
        c.execute('INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid, name))
        
        user_row = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        cards = [card_dict(r) for r in c.execute('SELECT cards.*,COALESCE(inventory.count,0) AS count FROM cards LEFT JOIN inventory ON cards.id=inventory.card_id AND inventory.user_id=? ORDER BY cards.id', (uid,))]
        
        query = '''
            SELECT u.id, SUM(CASE cards.rarity
                WHEN 'Обычная' THEN 100 WHEN 'Необычная' THEN 250
                WHEN 'Редкая' THEN 500 WHEN 'Эпическая' THEN 750
                WHEN 'Легендарная' THEN 1000 WHEN 'Мифическая' THEN 2500
                ELSE 0 END * inv.count) as total_rating
            FROM users u JOIN inventory inv ON u.id = inv.user_id
            JOIN cards ON inv.card_id = cards.id
            GROUP BY u.id HAVING total_rating > 0
            ORDER BY total_rating DESC
        '''
        all_users = c.execute(query).fetchall()
        
        total_players_with_rating = len(all_users)
        rank = total_players_with_rating + 1 
        for i, row in enumerate(all_users):
            if row['id'] == uid:
                rank = i + 1
                break

        last_puzzle = c.execute('SELECT started_at FROM sessions WHERE user_id=? AND kind=? AND done=1 ORDER BY started_at DESC LIMIT 1', (uid, 'puzzle')).fetchone()
        last_quotes = c.execute('SELECT started_at FROM sessions WHERE user_id=? AND kind=? AND done=1 ORDER BY started_at DESC LIMIT 1', (uid, 'quotes')).fetchone()
        
        puzzle_at = (last_puzzle['started_at'] + PUZZLE_COOLDOWN) if last_puzzle and last_puzzle['started_at'] else 0
        quotes_at = (last_quotes['started_at'] + QUOTES_COOLDOWN) if last_quotes and last_quotes['started_at'] else 0

        return {
            'name': user_row['name'], 
            'photo_url': tg_user.get('photo_url', ''),
            'dev': DEV_MODE, 
            'rating': sum(x['count']*x['points'] for x in cards), 
            'rank': rank,
            'total': sum(x['count'] for x in cards), 
            'unique': sum(x['count']>0 for x in cards), 
            'cards': cards, 
            'free_at': user_row['last_free'] + FREE_SECONDS, 
            'puzzle_at': puzzle_at,
            'quotes_at': quotes_at,
            'server_time': time.time()
        }

@app.get('/api/leaderboard')
def leaderboard(authorization: str | None = Header(default=None)):
    with db() as c:
        uid = player(c, authorization)
        query = '''
            SELECT u.id, u.name, u.photo_url, 
                   SUM(CASE cards.rarity
                       WHEN 'Обычная' THEN 100 WHEN 'Необычная' THEN 250
                       WHEN 'Редкая' THEN 500 WHEN 'Эпическая' THEN 750
                       WHEN 'Легендарная' THEN 1000 WHEN 'Мифическая' THEN 2500
                       ELSE 0 END * inv.count) as rating
            FROM users u JOIN inventory inv ON u.id = inv.user_id
            JOIN cards ON inv.card_id = cards.id
            GROUP BY u.id HAVING rating > 0
            ORDER BY rating DESC LIMIT 50
        '''
        rows = c.execute(query).fetchall()
        result = []
        for i, row in enumerate(rows):
            result.append({
                'rank': i + 1, 'name': row['name'], 'photo_url': row['photo_url'] or '',
                'rating': row['rating'], 'is_me': row['id'] == uid
            })
        return {'leaderboard': result}

@app.post('/api/free')
def free(authorization: str | None = Header(default=None)):
    with db() as c:
        uid = player(c,authorization)
        last = c.execute('SELECT last_free FROM users WHERE id=?',(uid,)).fetchone()[0]
        now = time.time()
        if now < last + FREE_SECONDS:
            raise HTTPException(429,'Бесплатная карта пока недоступна')
        result = reward(c,uid)
        c.execute('UPDATE users SET last_free=? WHERE id=?',(now,uid))
        return {'card':result}

def questions():
    items = json.loads((CONTENT_DIR / 'quotes.json').read_text(encoding='utf-8'))
    if len(items)<3: raise HTTPException(503,'Нужно минимум три вопроса')
    selected=[]
    for _ in range(3):
        q=secrets.choice(items)
        items.remove(q)
        if len(q['options'])!=4 or not 0<=q['correct']<4:
            raise HTTPException(503,'Неверный формат вопроса')
        selected.append(q)
    return selected

def public_session(s):
    state=json.loads(s['state'])
    result={'id':s['id'],'kind':s['kind'],'deadline':s['deadline'],'server_time':time.time()}
    if s['kind']=='quotes':
        q=state['questions'][state['round']]
        result.update(round=state['round']+1,text=q['text'],options=q['options'])
    return result

@app.post('/api/game/quotes/start')
def start_quotes(authorization: str | None = Header(default=None)):
    with db() as c:
        uid=player(c,authorization)
        last_q = c.execute('SELECT started_at FROM sessions WHERE user_id=? AND kind=? AND done=1 ORDER BY started_at DESC LIMIT 1', (uid, 'quotes')).fetchone()
        if last_q and last_q['started_at'] and time.time() < last_q['started_at'] + QUOTES_COOLDOWN:
            raise HTTPException(429, 'Цитаты пока недоступны')
            
        c.execute('DELETE FROM sessions WHERE deadline < ?',(time.time()-86400,))
        active=c.execute('SELECT * FROM sessions WHERE user_id=? AND kind=? AND done=0 AND deadline>? ORDER BY deadline DESC LIMIT 1',(uid,'quotes',time.time())).fetchone()
        if active: return public_session(active)
        
        c.execute('UPDATE sessions SET done=1 WHERE user_id=? AND kind=? AND done=0',(uid,'quotes'))
        state={'round':0,'questions':questions()}
        sid=secrets.token_urlsafe(24)
        now = time.time()
        deadline = now + 15
        c.execute('INSERT INTO sessions (id, user_id, kind, state, deadline, done, started_at) VALUES(?,?,?,?,?,0,?)',(sid,uid,'quotes',json.dumps(state,ensure_ascii=False),deadline, now))
        return public_session(c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())

class Answer(BaseModel):
    session_id: str = Field(max_length=100)
    choice: int = Field(ge=0,le=3)
    round: int = Field(default=1,ge=1,le=3)

@app.post('/api/game/answer')
def answer(body: Answer, authorization: str | None = Header(default=None)):
    with db() as c:
        uid=player(c,authorization)
        s=c.execute('SELECT * FROM sessions WHERE id=? AND user_id=?',(body.session_id,uid)).fetchone()
        if not s or s['done']:
            raise HTTPException(409,'Попытка уже завершена. Начни новую игру.')
        state=json.loads(s['state'])
        if time.time()>s['deadline']:
            c.execute('UPDATE sessions SET done=1 WHERE id=?',(s['id'],))
            return {'status':'lost','reason':'Время вышло'}
        if body.round != state['round']+1:
            raise HTTPException(409,'Этот раунд уже обработан. Открой игру заново.')
        q=state['questions'][state['round']]
        if body.choice!=q['correct']:
            c.execute('UPDATE sessions SET done=1 WHERE id=?',(s['id'],))
            return {'status':'lost','reason':'Неверный ответ','correct':q['options'][q['correct']]}
        if state['round']==2:
            c.execute('UPDATE sessions SET done=1, started_at=? WHERE id=?',(time.time(), s['id'],))
            return {'status':'won','card':reward(c,uid)}
        state['round']+=1
        c.execute('UPDATE sessions SET state=?,deadline=? WHERE id=?',(json.dumps(state,ensure_ascii=False),time.time()+15,s['id']))
        return {'status':'next','session':public_session(c.execute('SELECT * FROM sessions WHERE id=?',(s['id'],)).fetchone())}

# ==========================================
# ПАЗЛ
# ==========================================
class PuzzleStart(BaseModel):
    level: int = 1

@app.post('/api/game/puzzle/start')
def start_puzzle(body: PuzzleStart = None, authorization: str | None = Header(default=None)):
    if body is None: body = PuzzleStart(level=1)
    with db() as c:
        uid = player(c, authorization)
        last_p = c.execute('SELECT started_at FROM sessions WHERE user_id=? AND kind=? AND done=1 ORDER BY started_at DESC LIMIT 1', (uid, 'puzzle')).fetchone()
        if last_p and last_p['started_at'] and time.time() < last_p['started_at'] + PUZZLE_COOLDOWN:
            raise HTTPException(429, 'Пазл пока недоступен')
        
        c.execute('UPDATE sessions SET done=1, started_at=? WHERE user_id=? AND kind=? AND done=0', (time.time(), uid, 'puzzle'))
        
        # 1. Выбираем карту с учетом шансов (как в бесплатных картах)
        available_cards = c.execute('SELECT * FROM cards WHERE image != ""').fetchall()
        if not available_cards: 
            raise HTTPException(404, 'Нет карт с картинками в базе')
            
        weights = [DROP_WEIGHTS.get(card['rarity'], 1) for card in available_cards]
        card = random.choices(available_cards, weights=weights, k=1)[0]
            
        # 2. Настраиваем сетки и время под них
        pieces_map = {
            'Обычная': 3, 
            'Необычная': 3, 
            'Редкая': 3,      # 3 на 3
            'Эпическая': 4,   # 4 на 4
            'Легендарная': 5, # 5 на 5
            'Мифическая': 5   # <--- ИЗМЕНЕНО НА 5 (как у леги)
        }
        grid_size = pieces_map.get(card['rarity'], 3)
        
        img_path = BASE / card['image'].lstrip('/')
        if not img_path.exists(): raise HTTPException(404, f"Файл не найден: {card['image']}")
            
        img = Image.open(img_path).convert("RGBA")
        width, height = img.size
        min_dim = min(width, height)
        img = img.crop(((width-min_dim)/2, (height-min_dim)/2, (width+min_dim)/2, (height+min_dim)/2))
        
        piece_w = min_dim // grid_size
        piece_h = min_dim // grid_size
        pieces_b64 = []
        for r in range(grid_size):
            for c_idx in range(grid_size):
                box = (c_idx*piece_w, r*piece_h, (c_idx+1)*piece_w, (r+1)*piece_h)
                piece = img.crop(box)
                buffered = io.BytesIO()
                piece.save(buffered, format="PNG")
                pieces_b64.append(f"data:image/png;base64,{base64.b64encode(buffered.getvalue()).decode()}")
                
        shuffled_indices = list(range(grid_size * grid_size))
        random.shuffle(shuffled_indices)
        state = {'card_id': card['id'], 'grid_size': grid_size, 'pieces': pieces_b64}
        
        # 3. Умный таймер
        if grid_size <= 3:
            time_limit = 30
        elif grid_size == 4:
            time_limit = 60
        else:
            time_limit = 90  # 90 секунд для 5х5 (и Лега, и Мифик)

        sid = secrets.token_urlsafe(24)
        now = time.time()
        deadline = now + time_limit
        c.execute('INSERT INTO sessions (id, user_id, kind, state, deadline, done, started_at) VALUES(?,?,?,?,?,0,?)', (sid, uid, 'puzzle', json.dumps(state), deadline, now))
                  
        return {'session_id': sid, 'pieces': pieces_b64, 'shuffled_order': shuffled_indices, 'grid_size': grid_size, 'deadline': deadline, 'server_time': time.time()}

@app.post('/api/game/puzzle/finish')
def finish_puzzle(body: dict, authorization: str | None = Header(default=None)):
    session_id = body.get('session_id')
    with db() as c:
        uid = player(c, authorization)
        s = c.execute('SELECT * FROM sessions WHERE id=? AND user_id=? AND done=0', (session_id, uid)).fetchone()
        if not s: raise HTTPException(404, 'Сессия не найдена')
            
        if time.time() > s['deadline']:
            c.execute('UPDATE sessions SET done=1, started_at=? WHERE id=?',(time.time(), s['id'],))
            raise HTTPException(408, 'Время вышло')
            
        state = json.loads(s['state'])
        card_id = state['card_id']
        c.execute('UPDATE sessions SET done=1, started_at=? WHERE id=?', (time.time(), s['id'],))
        card = c.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
        c.execute('INSERT INTO inventory(user_id, card_id, count) VALUES(?, ?, 1) ON CONFLICT(user_id, card_id) DO UPDATE SET count = count + 1', (uid, card_id))
        return {'status': 'won', 'card': card_dict(card)}

@app.post('/api/game/puzzle/cancel')
def cancel_puzzle(body: dict, authorization: str | None = Header(default=None)):
    session_id = body.get('session_id')
    with db() as c:
        uid = player(c, authorization)
        c.execute('UPDATE sessions SET done=1, started_at=? WHERE id=? AND user_id=?', (time.time(), session_id, uid))
        return {'status': 'cancelled'}
    
# ==========================================
# ОБМЕНЫ (TRADES)
# ==========================================

class TradeCreate(BaseModel):
    receiver_username: str
    sender_cards: list[int]

class TradeAnswer(BaseModel):
    trade_id: int
    receiver_cards: list[int]

@app.post('/api/trades/create')
def create_trade(body: TradeCreate, authorization: str | None = Header(default=None)):
    if not body.sender_cards:
        raise HTTPException(400, 'Выберите хотя бы одну карту')
        
    with db() as c:
        uid = player(c, authorization)
        
        search_term = body.receiver_username.strip().lstrip('@')
        receiver = c.execute('SELECT id, name FROM users WHERE LOWER(username) = LOWER(?)', (search_term,)).fetchone()
        
        if not receiver:
            raise HTTPException(404, f'Игрок с юзернеймом @{search_term} не найден. Убедитесь, что он заходил в игру.')
        if receiver['id'] == uid:
            raise HTTPException(400, 'Нельзя обмениваться с самим собой')
            
        for card_id in body.sender_cards:
            inv = c.execute('SELECT count FROM inventory WHERE user_id=? AND card_id=?', (uid, card_id)).fetchone()
            if not inv or inv['count'] < 1:
                raise HTTPException(400, 'У вас нет одной из выбранных карт')
                
        cards_str = ','.join(map(str, body.sender_cards))
        c.execute('''
            INSERT INTO trades (sender_id, receiver_id, sender_cards, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
        ''', (uid, receiver['id'], cards_str, time.time()))
        
        return {'status': 'ok'}

@app.get('/api/trades/incoming')
def incoming_trades(authorization: str | None = Header(default=None)):
    with db() as c:
        uid = player(c, authorization)
        rows = c.execute('''
            SELECT t.id as trade_id, u.name as sender_name, u.username as sender_username, u.photo_url as sender_photo, t.sender_cards
            FROM trades t
            JOIN users u ON t.sender_id = u.id
            WHERE t.receiver_id = ? AND t.status = 'pending'
            ORDER BY t.created_at DESC
        ''', (uid,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            card_ids = d['sender_cards'].split(',')
            cards_info = []
            for cid in card_ids:
                card = c.execute('SELECT * FROM cards WHERE id=?', (cid,)).fetchone()
                if card:
                    cd = card_dict(card)
                    cards_info.append(cd)
            d['cards'] = cards_info
            del d['sender_cards']
            result.append(d)
        return {'trades': result}

@app.post('/api/trades/answer')
def answer_trade(body: TradeAnswer, authorization: str | None = Header(default=None)):
    if not body.receiver_cards:
        raise HTTPException(400, 'Выберите хотя бы одну карту в ответ')
        
    with db() as c:
        uid = player(c, authorization)
        
        trade = c.execute('SELECT * FROM trades WHERE id=? AND receiver_id=? AND status="pending"', (body.trade_id, uid)).fetchone()
        if not trade:
            raise HTTPException(404, 'Обмен не найден или уже обработан')
            
        for card_id in body.receiver_cards:
            inv = c.execute('SELECT count FROM inventory WHERE user_id=? AND card_id=?', (uid, card_id)).fetchone()
            if not inv or inv['count'] < 1:
                raise HTTPException(400, 'У вас нет одной из выбранных карт')
                
        sender_card_ids = trade['sender_cards'].split(',')
        for cid in sender_card_ids:
            sender_inv = c.execute('SELECT count FROM inventory WHERE user_id=? AND card_id=?', (trade['sender_id'], cid)).fetchone()
            if not sender_inv or sender_inv['count'] < 1:
                raise HTTPException(400, 'У отправителя больше нет одной из этих карт')
                
        for cid in sender_card_ids:
            c.execute('UPDATE inventory SET count = count - 1 WHERE user_id=? AND card_id=?', (trade['sender_id'], cid))
            c.execute('INSERT INTO inventory (user_id, card_id, count) VALUES (?, ?, 1) ON CONFLICT(user_id, card_id) DO UPDATE SET count = count + 1', (uid, cid))
            
        for cid in body.receiver_cards:
            c.execute('UPDATE inventory SET count = count - 1 WHERE user_id=? AND card_id=?', (uid, cid))
            c.execute('INSERT INTO inventory (user_id, card_id, count) VALUES (?, ?, 1) ON CONFLICT(user_id, card_id) DO UPDATE SET count = count + 1', (trade['sender_id'], cid))
            
        receiver_cards_str = ','.join(map(str, body.receiver_cards))
        c.execute('UPDATE trades SET status="accepted", receiver_cards=? WHERE id=?', (receiver_cards_str, body.trade_id))
        
        return {'status': 'ok'}

@app.post('/api/trades/decline')
def decline_trade(body: dict, authorization: str | None = Header(default=None)):
    with db() as c:
        uid = player(c, authorization)
        trade_id = body.get('trade_id')
        trade = c.execute('SELECT * FROM trades WHERE id=? AND receiver_id=? AND status="pending"', (trade_id, uid)).fetchone()
        if trade:
            c.execute('UPDATE trades SET status="declined" WHERE id=?', (trade_id,))
        return {'status': 'ok'}

class CraftRequest(BaseModel):
    card_ids: list[int]
    target_rarity: str

@app.post('/api/craft')
def craft(body: CraftRequest, authorization: str | None = Header(default=None)):
    if body.target_rarity not in CRAFT_RECIPES:
        raise HTTPException(400, 'Такого рецепта не существует')

    recipe = CRAFT_RECIPES[body.target_rarity]
    source_rarity = recipe['from']
    amount_needed = recipe['amount']

    if len(body.card_ids) != amount_needed:
        raise HTTPException(400, f'Нужно выбрать ровно {amount_needed} карт')

    with db() as c:
        uid = player(c, authorization)
        
        # Считаем, сколько копий каждой карты мы пытаемся сдать
        counts = Counter(body.card_ids)
        
        for card_id, req_count in counts.items():
            card = c.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
            if not card or card['rarity'] != source_rarity:
                raise HTTPException(400, 'Неверная карта для этого рецепта')
            
            inv = c.execute('SELECT count FROM inventory WHERE user_id=? AND card_id=?', (uid, card_id)).fetchone()
            
            # Проверка: у игрока должно быть достаточно карт, И ПОСЛЕ КРАФТА ДОЛЖНА ОСТАТЬСЯ ХОТЯ БЫ 1
            if not inv or inv['count'] - req_count < 1:
                raise HTTPException(400, 'Нельзя сдавать последнюю копию карты (можно только повторки)')

        # Списываем карты
        for card_id in body.card_ids:
            c.execute('UPDATE inventory SET count = count - 1 WHERE user_id=? AND card_id=?', (uid, card_id))

        # Выдаем случайную карту
        new_card = c.execute('SELECT * FROM cards WHERE rarity=? ORDER BY RANDOM() LIMIT 1', (body.target_rarity,)).fetchone()
        c.execute('INSERT INTO inventory(user_id, card_id, count) VALUES(?, ?, 1) ON CONFLICT(user_id, card_id) DO UPDATE SET count = count + 1', (uid, new_card['id']))

        return {'status': 'ok', 'card': card_dict(new_card)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)