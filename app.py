import csv, json, math, os, re, secrets, sqlite3
from functools import wraps, lru_cache
from collections import defaultdict, Counter
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash

ROOT = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(ROOT, 'models')
DB = os.path.join(ROOT, 'cinereco.db')
CATALOG_FILE = os.path.join(MOD, 'catalog.csv')
POSTER_DIR = os.path.join(ROOT, 'static', 'posters')
ACTOR_INFO_FILE = os.path.join(MOD, 'actor_info.json')
SIMILAR_CACHE_FILE = os.path.join(MOD, 'similar_cache.json')

app = Flask(__name__)
app.secret_key = os.environ.get('CINERECO_SECRET_KEY', 'cinereco-local-dev-key-change-me')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 30
app.config['TEMPLATES_AUTO_RELOAD'] = False

# ---------------- Local-only catalogue engine ----------------
# Runtime deliberately uses only Python stdlib + Flask. Heavy ML packages are
# not needed after the model-building stage, which keeps startup fast and avoids
# NumPy/SciPy wheel problems on Windows.

def to_float(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default

def to_int(v, default=0):
    try: return int(float(v))
    except (TypeError, ValueError): return default

def split_pipe(v):
    return [x.strip() for x in str(v or '').split('|') if x.strip() and x.strip() != '(no genres listed)']

def tokenize(text):
    return [x for x in re.findall(r"[a-z0-9]+", str(text or '').lower()) if len(x) > 1]

CATALOG = []
BY_ID = {}
INVERTED = defaultdict(set)
TOKEN_DF = Counter()
TOKEN_COUNT = 0

with open(CATALOG_FILE, newline='', encoding='utf-8') as f:
    for r in csv.DictReader(f):
        m = {
            'movieId': to_int(r.get('movieId')),
            'title': r.get('title',''),
            'genres': [x for x in re.split(r'[|,\s]+', str(r.get('genres','')).strip()) if x and x.lower() not in {'(no','genres','listed)'}],
            'imdbId': r.get('imdbId',''),
            'cast': split_pipe(r.get('cast'))[:12],
            'director': split_pipe(r.get('director'))[:3],
            'tags': tokenize(r.get('tags'))[:32],
            'poster_file': r.get('poster_file',''),
            'rating_mean': to_float(r.get('rating_mean')),
            'rating_count': to_int(r.get('rating_count')),
            'bayes': to_float(r.get('bayes')),
        }
        m['year'] = int(x.group(1)) if (x := re.search(r'\((\d{4})\)', m['title'])) else 0
        CATALOG.append(m); BY_ID[m['movieId']] = m

ACTOR_INFO = {}
try:
    with open(ACTOR_INFO_FILE, encoding='utf-8') as _af:
        ACTOR_INFO = {str(x.get('name','')).casefold(): x for x in json.load(_af) if x.get('name')}
except (OSError, json.JSONDecodeError):
    ACTOR_INFO = {}


# Fast prefix index for live search suggestions. Suggestions no longer scan all 62K
# movies on every keystroke.
SUGGEST_INDEX = defaultdict(set)
for _m in CATALOG:
    _search_parts = [_m['title'], *_m['genres'], *_m['cast'][:8], *_m['director'][:3], *_m['tags'][:24]]
    _seen_tokens = set()
    for _part in _search_parts:
        for _tok in tokenize(_part):
            if _tok in _seen_tokens: continue
            _seen_tokens.add(_tok)
            if len(_tok) >= 2:
                SUGGEST_INDEX[_tok[:2]].add(_m['movieId'])

ALL_GENRES = tuple(sorted({g for _m in CATALOG for g in _m['genres']}))

# Persisted similarity cache is loaded at runtime when available. It is generated
# offline for the most popular poster-backed films, avoiding a cold calculation
# when users click common home-page cards.
SIMILAR_CACHE = {}
try:
    with open(SIMILAR_CACHE_FILE, encoding='utf-8') as _sf:
        _raw_cache = json.load(_sf)
        SIMILAR_CACHE = {int(k): tuple(int(x) for x in v) for k, v in _raw_cache.items()}
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    SIMILAR_CACHE = {}

def cast_people_for(m):
    people=[]; seen=set()
    for name in (m.get('cast') or [])[:8]:
        key=str(name).strip().casefold()
        if not key or key in seen: continue
        seen.add(key)
        info=ACTOR_INFO.get(key,{})
        people.append({
            'name': str(name).strip(),
            'alternative_name': info.get('alternative_name'),
            'rating': to_int(info.get('rating'),0),
            'initial': str(name).strip()[:1].upper() or '•'
        })
    return people

# Build a lightweight content index. This replaces runtime NumPy/SciPy/sklearn.
for m in CATALOG:
    seen = set()
    for prefix, values in [('g', m['genres']), ('d', m['director']), ('c', m['cast']), ('t', m['tags'])]:
        for value in values:
            for tok in tokenize(value):
                key = prefix + ':' + tok
                if key not in seen:
                    INVERTED[key].add(m['movieId']); seen.add(key)
    for key in seen: TOKEN_DF[key] += 1
TOKEN_COUNT = len(CATALOG)

# Precompute per-movie feature norms once at startup. The previous implementation
# recalculated every candidate's norm during a movie-detail request, which made
# opening a movie unnecessarily slow.
FEATURE_WEIGHTS = {'g': 4.0, 'd': 7.0, 'c': 2.2, 't': 1.0}
MOVIE_NORMS = {}
for _m in CATALOG:
    _sum = 0.0
    _seen = set()
    for _prefix, _values in [('g',_m['genres']),('d',_m['director']),('c',_m['cast']),('t',_m['tags'])]:
        for _value in _values:
            for _tok in tokenize(_value):
                _key = _prefix + ':' + _tok
                if _key in _seen: continue
                _seen.add(_key)
                _df = TOKEN_DF.get(_key, 0)
                if _df:
                    _z = FEATURE_WEIGHTS[_prefix] * (math.log((TOKEN_COUNT+1)/(_df+1)) + 1)
                    _sum += _z * _z
    MOVIE_NORMS[_m['movieId']] = math.sqrt(_sum) or 1.0



def db():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=20000')
    return c

def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER,movie_id INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,movie_id));
    CREATE TABLE IF NOT EXISTS wishlist(user_id INTEGER,movie_id INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,movie_id));
    CREATE TABLE IF NOT EXISTS playlists(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,name TEXT NOT NULL,description TEXT DEFAULT '',public INTEGER DEFAULT 1,token TEXT UNIQUE NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS playlist_movies(playlist_id INTEGER,movie_id INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(playlist_id,movie_id));
    CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER,movie_id INTEGER,rating REAL NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,movie_id));
    CREATE TABLE IF NOT EXISTS watched(user_id INTEGER,movie_id INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,movie_id));
    CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,action TEXT NOT NULL,movie_id INTEGER,details TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(user_id,created_at DESC);
    '''); c.commit(); c.close()

@lru_cache(maxsize=1024)
def similar_ids(mid, n=12):
    mid = int(mid)
    cached = SIMILAR_CACHE.get(mid)
    if cached:
        return tuple(x for x in cached if x in BY_ID)[:n]
    m = BY_ID.get(mid)
    if not m: return tuple(x['movieId'] for x in popular(n))
    # Candidate retrieval from shared genre/director/cast/tag tokens.
    weights = FEATURE_WEIGHTS
    scores = defaultdict(float)
    norm_a = 0.0
    for prefix, values in [('g',m['genres']),('d',m['director']),('c',m['cast']),('t',m['tags'])]:
        for value in values:
            for tok in tokenize(value):
                key = prefix+':'+tok; df=TOKEN_DF.get(key,0)
                if df <= 0: continue
                idf = math.log((TOKEN_COUNT+1)/(df+1))+1
                w = weights[prefix]*idf
                norm_a += w*w
                for cid in INVERTED.get(key,()):
                    if cid != mid: scores[cid] += w
    denom_a = math.sqrt(norm_a) or 1.0
    ranked=[]
    for cid, dot in scores.items():
        cm=BY_ID[cid]
        cosine = dot / (denom_a * MOVIE_NORMS.get(cid, 1.0))
        score = cosine + 0.015*cm['bayes']
        ranked.append((score,cid))
    ranked.sort(reverse=True)
    return tuple(cid for _,cid in ranked[:n])

def movie(mid): return BY_ID.get(to_int(mid))

def has_local_poster(m):
    name = m.get('poster_file','')
    return bool(name) and os.path.isfile(os.path.join(POSTER_DIR, name))

@lru_cache(maxsize=256)
def popular(n=12, genre=None, poster_only=False):
    pool=[m for m in CATALOG if not genre or any(genre.lower()==g.lower() for g in m['genres'])]
    if poster_only:
        with_posters=[m for m in pool if has_local_poster(m)]
        if len(with_posters) >= n:
            pool=with_posters
    # Prefer titles with meaningful rating volume, but don't hide the catalogue if the pool is small.
    strong=[m for m in pool if m['rating_count']>=100]
    if len(strong)>=n: pool=strong
    return sorted(pool,key=lambda m:(m['bayes'],m['rating_count']),reverse=True)[:n]

@lru_cache(maxsize=8)
def newest_movies(n=10):
    return tuple(sorted(CATALOG,key=lambda m:m['year'],reverse=True)[:n])

def for_you(user_id,n=12):
    """Return resilient local recommendations; bad/stale IDs are ignored."""
    try:
        if not user_id:
            return popular(n)
        c=db()
        fav=[int(r['movie_id']) for r in c.execute('SELECT movie_id FROM favorites WHERE user_id=?',(user_id,))]
        wish=[int(r['movie_id']) for r in c.execute('SELECT movie_id FROM wishlist WHERE user_id=?',(user_id,))]
        high=[int(r['movie_id']) for r in c.execute('SELECT movie_id FROM ratings WHERE user_id=? AND rating>=4',(user_id,))]
        c.close()
        seeds=[sid for sid in dict.fromkeys(fav+wish+high) if sid in BY_ID]
        if not seeds:
            return popular(n)
    except Exception:
        return popular(n)
    if not seeds: return popular(n)
    scores=Counter()
    for seed in seeds[:12]:
        for rank,cid in enumerate(similar_ids(seed,16)):
            scores[cid]+=max(1.0,16-rank)
    for sid in seeds: scores.pop(sid,None)
    ranked=sorted(scores,key=lambda x:(scores[x],BY_ID[x]['bayes']),reverse=True)
    result=[BY_ID[x] for x in ranked[:n]]
    if len(result)<n:
        seen={m['movieId'] for m in result}|set(seeds)
        result += [m for m in popular(n*2) if m['movieId'] not in seen][:n-len(result)]
    return result

def logged(f):
    @wraps(f)
    def w(*a,**k):
        if 'user_id' not in session:return redirect(url_for('login',next=request.path))
        return f(*a,**k)
    return w

def log_activity(user_id, action, movie_id=None, details=''):
    c=db(); c.execute('INSERT INTO activity(user_id,action,movie_id,details) VALUES(?,?,?,?)',(user_id,action,movie_id,details)); c.commit(); c.close()

def user_has(table,mid):
    if not session.get('user_id'): return False
    c=db(); row=c.execute(f'SELECT 1 FROM {table} WHERE user_id=? AND movie_id=?',(session['user_id'],mid)).fetchone(); c.close(); return bool(row)

@app.context_processor
def ctx(): return {'current_user':session.get('user_name'),'current_user_id':session.get('user_id')}

@app.route('/')
def home():
    uid=session.get('user_id'); pop=popular(12, poster_only=True); fy=for_you(uid,10) if uid else pop[:10]
    featured=pop[0] if pop else popular(1)[0]
    return render_template('home.html',featured=featured,foryou=fy,trending=pop,newest=newest_movies(10))

@app.route('/api/suggest')
def suggestions():
    q=request.args.get('q','').strip().lower()
    if len(q)<2:
        return jsonify([])
    parts=tokenize(q)
    buckets=[]
    for p in parts:
        buckets.append(SUGGEST_INDEX.get(p[:2], set()))
    candidate_ids=set().union(*buckets) if buckets else set()
    # For very short/rare queries, fall back to titles beginning with the query.
    if not candidate_ids:
        candidate_ids={m['movieId'] for m in CATALOG if m['title'].lower().startswith(q)}
    out=[]
    for mid in candidate_ids:
        m=BY_ID.get(mid)
        if not m: continue
        hay_title=m['title'].lower(); hay_gen=' '.join(m['genres']).lower(); hay_tags=' '.join(m['tags']).lower(); hay_cast=' '.join(m['cast'][:8]).lower(); hay_dir=' '.join(m['director'][:3]).lower()
        exact= q == hay_title
        starts=hay_title.startswith(q)
        contains= q in hay_title or q in hay_gen or q in hay_tags or q in hay_cast or q in hay_dir
        if parts and not contains and not all(any(part in h for h in (hay_title,hay_gen,hay_tags,hay_cast,hay_dir)) for part in parts):
            continue
        rank=(1000 if exact else 500 if starts else 100 if q in hay_title else 60)+m['bayes']
        out.append((rank,m))
    out.sort(key=lambda x:(x[0],x[1]['rating_count']),reverse=True)
    return jsonify([{'movieId':m['movieId'],'title':m['title'],'genres':m['genres'][:2],'rating':round(m['rating_mean'],1)} for _,m in out[:8]])

@app.route('/discover')
def discover():
    q=request.args.get('q','').strip().lower(); genre=request.args.get('genre','').strip(); sort=request.args.get('sort','popular'); min_rating=to_float(request.args.get('min_rating'),0); year_from=to_int(request.args.get('year_from'),0); year_to=to_int(request.args.get('year_to'),9999)
    d=CATALOG
    if q: d=[m for m in d if q in m['title'].lower() or q in ' '.join(m['genres']).lower() or q in ' '.join(m['tags']).lower() or q in ' '.join(m['cast']).lower() or q in ' '.join(m['director']).lower()]
    if genre:d=[m for m in d if any(genre.lower()==g.lower() for g in m['genres'])]
    if min_rating:d=[m for m in d if m['rating_mean']>=min_rating]
    d=[m for m in d if year_from<=m['year']<=year_to]
    if sort=='rating': d=sorted(d,key=lambda m:(m['rating_mean'],m['rating_count']),reverse=True)
    elif sort=='new': d=sorted(d,key=lambda m:m['year'],reverse=True)
    else:d=sorted(d,key=lambda m:(m['bayes'],m['rating_count']),reverse=True)
    genres=ALL_GENRES
    return render_template('discover.html',movies=d[:48],genres=genres,q=q,genre=genre,sort=sort,min_rating=request.args.get('min_rating',''),year_from=request.args.get('year_from',''),year_to=request.args.get('year_to',''))

@app.route('/for-you')
def for_you_page():
    uid=session.get('user_id')
    movies=for_you(uid,24)
    rating_buckets=[]
    if uid:
        c=db()
        rows=c.execute('SELECT movie_id,rating FROM ratings WHERE user_id=? ORDER BY created_at DESC',(uid,)).fetchall()
        c.close()
        for star in (5,4,3,2,1):
            ids=[int(r['movie_id']) for r in rows if abs(float(r['rating'])-star)<0.01 and int(r['movie_id']) in BY_ID]
            rating_buckets.append({'star':star,'count':len(ids),'movies':[BY_ID[i] for i in ids[:8]]})
    return render_template('for_you.html',movies=movies,personalized=bool(uid),rating_buckets=rating_buckets,rating_total=sum(b['count'] for b in rating_buckets))
@app.route('/favorites')
@logged
def favorites_page():
    c=db(); ms=[movie(r['movie_id']) for r in c.execute('SELECT movie_id FROM favorites WHERE user_id=? ORDER BY created_at DESC',(session['user_id'],))]; c.close(); return render_template('library.html',title='Favorites',eyebrow='YOUR FAVORITES',movies=ms,empty='Save films you love and they will appear here.')
@app.route('/wishlist')
@logged
def wishlist_page():
    c=db(); ms=[movie(r['movie_id']) for r in c.execute('SELECT movie_id FROM wishlist WHERE user_id=? ORDER BY created_at DESC',(session['user_id'],))]; c.close(); return render_template('library.html',title='Wishlist',eyebrow='WATCH LATER',movies=ms,empty='Your wishlist is waiting.')

@app.route('/movie/<int:mid>')
def detail(mid):
    m=movie(mid)
    if not m:return render_template('404.html'),404
    user_playlists=[]; user_rating=None
    if session.get('user_id'):
        c=db(); user_playlists=c.execute('SELECT id,name FROM playlists WHERE user_id=? ORDER BY created_at DESC',(session['user_id'],)).fetchall(); rr=c.execute('SELECT rating FROM ratings WHERE user_id=? AND movie_id=?',(session['user_id'],mid)).fetchone(); user_rating=float(rr['rating']) if rr else None; c.close()
    return render_template('movie.html',m=m,recs=[BY_ID[x] for x in similar_ids(mid,10)],cast_people=cast_people_for(m),user_playlists=user_playlists,user_rating=user_rating,is_favorite=user_has('favorites',mid),is_wishlisted=user_has('wishlist',mid),is_watched=user_has('watched',mid))

@app.route('/api/recommend/<int:mid>')
def api_rec(mid):return jsonify([BY_ID[x] for x in similar_ids(mid,12)])

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form['email'].strip().lower(); p=request.form['password']; c=db(); u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); c.close()
        if u and check_password_hash(u['password'],p): session['user_id']=u['id'];session['user_name']=u['name'];return redirect(request.args.get('next') or url_for('home'))
        flash('Email or password is incorrect.','error')
    return render_template('auth.html',mode='login')
@app.route('/register',methods=['GET','POST'])
def register():
    if request.method=='POST':
        name=request.form['name'].strip(); email=request.form['email'].strip().lower(); p=request.form['password']
        if len(p)<6:flash('Use at least 6 characters.','error');return render_template('auth.html',mode='register')
        try:
            c=db();cur=c.execute('INSERT INTO users(name,email,password) VALUES(?,?,?)',(name,email,generate_password_hash(p)));c.commit();session['user_id']=cur.lastrowid;session['user_name']=name;c.close();return redirect(url_for('home'))
        except sqlite3.IntegrityError: flash('That email is already registered.','error')
    return render_template('auth.html',mode='register')
@app.route('/logout')
def logout():session.clear();return redirect(url_for('home'))

def toggle(table,mid,action_label):
    uid=session['user_id']; c=db(); exists=c.execute(f'SELECT 1 FROM {table} WHERE user_id=? AND movie_id=?',(uid,mid)).fetchone()
    if exists:c.execute(f'DELETE FROM {table} WHERE user_id=? AND movie_id=?',(uid,mid));on=False
    else:c.execute(f'INSERT INTO {table}(user_id,movie_id) VALUES(?,?)',(uid,mid));on=True
    c.commit();c.close();log_activity(uid,('removed_' if not on else '')+action_label,mid);return on

def action_response(on,label,mid):
    if request.headers.get('X-Requested-With')=='XMLHttpRequest' or request.is_json:
        return jsonify(ok=True,active=bool(on),label=label)
    flash(label,'success')
    return redirect(request.referrer or url_for('detail',mid=mid))

@app.post('/movie/<int:mid>/favorite')
@logged
def favorite(mid):
    on=toggle('favorites',mid,'favorite')
    return action_response(on,'Added to favorites' if on else 'Removed from favorites',mid)

@app.post('/movie/<int:mid>/wishlist')
@logged
def wish(mid):
    on=toggle('wishlist',mid,'wishlist')
    return action_response(on,'Added to wishlist' if on else 'Removed from wishlist',mid)

@app.post('/movie/<int:mid>/watched')
@logged
def watched_movie(mid):
    on=toggle('watched',mid,'watched')
    return action_response(on,'Marked as watched' if on else 'Marked as unwatched',mid)
@app.post('/movie/<int:mid>/rate')
@logged
def rate_movie(mid):
    if not movie(mid):return render_template('404.html'),404
    rating=to_float(request.form.get('rating'),0)
    if rating<0.5 or rating>5 or abs(rating*2-round(rating*2))>1e-9: flash('Choose a rating from 0.5 to 5 stars.','error');return redirect(request.referrer or url_for('detail',mid=mid))
    c=db();c.execute('INSERT INTO ratings(user_id,movie_id,rating) VALUES(?,?,?) ON CONFLICT(user_id,movie_id) DO UPDATE SET rating=excluded.rating,created_at=CURRENT_TIMESTAMP',(session['user_id'],mid,rating));c.commit();c.close();log_activity(session['user_id'],'rated',mid,f'{rating:.1f} stars')
    if request.headers.get('X-Requested-With')=='XMLHttpRequest':
        return jsonify(ok=True,rating=rating,label=f'Your {rating:.1f}★ rating was saved.')
    flash(f'Your {rating:.1f}★ rating was saved.','success');return redirect(request.referrer or url_for('detail',mid=mid))
@app.post('/movie/<int:mid>/rate/remove')
@logged
def remove_rating(mid):
    c=db();c.execute('DELETE FROM ratings WHERE user_id=? AND movie_id=?',(session['user_id'],mid));c.commit();c.close();log_activity(session['user_id'],'removed_rating',mid);return jsonify(ok=True,active=False,label='Rating removed') if request.headers.get('X-Requested-With')=='XMLHttpRequest' else redirect(request.referrer or url_for('detail',mid=mid))

@app.route('/profile')
@logged
def profile():
    uid=session['user_id']
    selected_rating=to_float(request.args.get('rating'),0)
    c=db()
    favorites=[movie(r['movie_id']) for r in c.execute('SELECT movie_id FROM favorites WHERE user_id=? ORDER BY created_at DESC',(uid,))]
    wishlist=[movie(r['movie_id']) for r in c.execute('SELECT movie_id FROM wishlist WHERE user_id=? ORDER BY created_at DESC',(uid,))]
    watched=[movie(r['movie_id']) for r in c.execute('SELECT movie_id FROM watched WHERE user_id=? ORDER BY created_at DESC',(uid,))]
    rating_rows=[(movie(r['movie_id']),float(r['rating']),r['created_at']) for r in c.execute('SELECT movie_id,rating,created_at FROM ratings WHERE user_id=? ORDER BY created_at DESC',(uid,))]
    activity=c.execute('SELECT action,movie_id,details,created_at FROM activity WHERE user_id=? ORDER BY created_at DESC LIMIT 80',(uid,)).fetchall()
    playlists=c.execute('SELECT * FROM playlists WHERE user_id=? ORDER BY created_at DESC',(uid,)).fetchall()
    c.close()
    ratings=[x for x in rating_rows if x[0]]
    rating_buckets=[]
    for star in (5,4,3,2,1):
        items=[x for x in ratings if abs(x[1]-star)<0.01]
        rating_buckets.append({'star':star,'count':len(items),'movies':[x[0] for x in items]})
    filtered_ratings=[x for x in ratings if not selected_rating or abs(x[1]-selected_rating)<0.01]
    return render_template('profile.html',favorites=[x for x in favorites if x],wishlist=[x for x in wishlist if x],
                           watched=[x for x in watched if x],ratings=filtered_ratings,all_ratings=ratings,
                           rating_buckets=rating_buckets,selected_rating=selected_rating,
                           activity=activity,playlists=playlists)

@app.route('/playlists',methods=['GET','POST'])
@logged
def playlists():
    c=db()
    if request.method=='POST':
        name=request.form['name'].strip();desc=request.form.get('description','').strip();public=1 if request.form.get('public') else 0
        if name:c.execute('INSERT INTO playlists(user_id,name,description,public,token) VALUES(?,?,?,?,?)',(session['user_id'],name,desc,public,secrets.token_urlsafe(9)));c.commit();log_activity(session['user_id'],'created_playlist',None,name)
    ps=c.execute('SELECT * FROM playlists WHERE user_id=? ORDER BY created_at DESC',(session['user_id'],)).fetchall();c.close();return render_template('playlists.html',playlists=ps)
@app.post('/playlist/add/<int:mid>')
@logged
def add_playlist(mid):
    try:pid=int(request.form.get('playlist_id',''))
    except ValueError:pid=0
    c=db();ok=c.execute('SELECT 1 FROM playlists WHERE id=? AND user_id=?',(pid,session['user_id'])).fetchone()
    if ok:c.execute('INSERT OR IGNORE INTO playlist_movies(playlist_id,movie_id) VALUES(?,?)',(pid,mid));c.commit();log_activity(session['user_id'],'added_to_playlist',mid);flash('Added to playlist.','success')
    else:flash('Choose one of your playlists first.','error')
    c.close();return redirect(request.referrer or url_for('detail',mid=mid))
@app.route('/playlist/<token>')
def public_playlist(token):
    c=db();p=c.execute('SELECT * FROM playlists WHERE token=?',(token,)).fetchone()
    if not p or (not p['public'] and p['user_id']!=session.get('user_id')):c.close();return render_template('404.html'),404
    ms=[movie(r['movie_id']) for r in c.execute('SELECT movie_id FROM playlist_movies WHERE playlist_id=? ORDER BY created_at',(p['id'],))];c.close();return render_template('public_playlist.html',playlist=p,movies=ms,owner=(p['user_id']==session.get('user_id')))

@app.after_request
def cache_static(response):
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
    return response

@app.route('/health')
def health():return jsonify(status='ok',movies=len(CATALOG),external_api=False,similar_cache=len(SIMILAR_CACHE))
@app.errorhandler(500)
def internal_error(e):
    app.logger.exception('CineReco internal error');return render_template('500.html'),500

init_db()
if __name__=='__main__':
    port=int(os.environ.get('CINERECO_PORT','5080'));app.run(host='127.0.0.1',port=port,debug=False)
