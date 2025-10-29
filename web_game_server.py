import os, json, time, random, string, threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from flask import Flask, request, send_from_directory, session, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room, leave_room

# ====== Trie ======
try:
    import sys
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    sys.path.append(BASE_DIR)
    sys.path.append(os.path.join(BASE_DIR, "core"))
    from core.trie import Trie
except Exception:
    class Trie:
        def __init__(self): self.words=set()
        def insert(self,w:str): self.words.add(w)
        def starts_with(self,p:str)->bool: return any(w.startswith(p) for w in self.words)
        def matches_prefix(self,p:str)->List[str]: return [w for w in self.words if w.startswith(p)]
        def remove(self,w:str): self.words.discard(w)

# ====== קונפיג ======
CANVAS_W, CANVAS_H = 1200, 800
TICK_HZ = 30
SPAWN_EVERY_MIN = 0.6
SPAWN_EVERY_MAX = 1.2
WORD_SPEED_BASE = 1.4
WORD_SPEED_RAND = 0.6
PLAYER_LIVES = 3
SCORE_PER_CHAR = 5
WORD_BONUS = 10
STREAK_BONUS = 4
MISS_LIFE_PENALTY = 1
PLAYER_COLORS = [(255,120,120),(120,120,255)]
WORD_BANK_PATHS = ["./assets/wordcache_en.json", "./wordcache_en.json"]

# ====== Flask + SocketIO ======
app = Flask(__name__, static_folder=".", template_folder="templates")
app.config["SECRET_KEY"] = "space-typing-secret"
socketio = SocketIO(app, cors_allowed_origins="*", supports_credentials=True)

# ====== טעינת מאגר מילים ======
def load_word_bank() -> List[str]:
    words: List[str] = []
    for p in WORD_BANK_PATHS:
        if os.path.exists(p):
            try:
                data = json.load(open(p, "r", encoding="utf-8"))
                if isinstance(data, list):
                    words = data
                elif isinstance(data, dict) and "words" in data:
                    words = data["words"]
                elif isinstance(data, dict) and "buckets" in data:
                    # טיפול בפורמט buckets של wordcache_en.json
                    words = []
                    for bucket_words in data["buckets"].values():
                        if isinstance(bucket_words, list):
                            words.extend(bucket_words)
                break
            except Exception:
                pass
    if not words:
        words = ["space","typing","galaxy","planet","rocket","comet","meteor","python","socket","vector","engine"]
    clean = []
    seen = set()
    for w in words:
        if not isinstance(w, str): continue
        w = w.strip().lower()
        if not w or not w.isalpha(): continue
        if w in seen: continue
        seen.add(w); clean.append(w)
    return clean

WORD_BANK = load_word_bank()

# ====== מודלים ======
@dataclass
class Player:
    sid: str
    username: str
    color: Tuple[int,int,int]
    score: int = 0
    lives: int = PLAYER_LIVES
    streak: int = 0
    current_word_id: Optional[str] = None
    ready: bool = False

@dataclass
class Word:
    id: str
    text: str
    x: float
    y: float
    speed: float
    status: str = "falling"
    owner_sid: Optional[str] = None
    typed: str = ""
    remaining: str = ""

    def to_public(self, players: Dict[str, Player]) -> dict:
        d = {
            "id": self.id, "text": self.text, "x": self.x, "y": self.y,
            "typed": self.typed, "remaining": self.remaining, "status": self.status,
            "claimed_by": self.owner_sid if self.status == "completed" else None,
            "typing_player": self.owner_sid if self.status in ("locked","completed") else None,
        }
        if self.owner_sid and self.owner_sid in players:
            col = players[self.owner_sid].color
            d["player_color"] = col
            d["locked_by_color"] = col
        else:
            d["player_color"] = None
            d["locked_by_color"] = None
        return d

# ====== מחלקת המשחק ======
class Game:
    def __init__(self, room_id: str, words_list: List[str]):
        self.room_id = room_id
        self.players: Dict[str, Player] = {}
        self.words: Dict[str, Word] = {}
        self.words_list = words_list
        self.trie = Trie()
        for w in self.words_list:
            if isinstance(w, str) and w.isalpha():
                self.trie.insert(w)
        self.running = False
        self.lock = threading.Lock()
        self.rng = random.Random(time.time()+hash(room_id))
        self.active_texts: set[str] = set()
        self.used_words: set[str] = set()  # מילים שכבר הופיעו במשחק
        self.next_spawn_time = time.time() + self.rng.uniform(SPAWN_EVERY_MIN, SPAWN_EVERY_MAX)

    def public_players(self):
        """מחזיר את המידע הציבורי של השחקנים - כל שחקן רואה את הניקוד שלו ואת הניקוד של היריב"""
        result = {}
        player_sids = list(self.players.keys())
        
        for sid, p in self.players.items():
            # מצא את היריב
            opponent_score = 0
            if len(player_sids) == 2:
                opponent_sid = player_sids[1] if sid == player_sids[0] else player_sids[0]
                opponent_score = self.players[opponent_sid].score
            
            result[sid] = {
                "username": p.username, 
                "score": p.score, 
                "lives": p.lives,
                "my_score": p.score,  # הניקוד שלי
                "opponent_score": opponent_score   # הניקוד של היריב
            }
        
        return result

    def snapshot(self):
        return [w.to_public(self.players) for w in self.words.values()]

    def _choose_unique_text(self) -> str:
        # נסה למצוא מילה ייחודית שלא הופיעה עדיין
        available_words = []
        for word in self.words_list:
            if word not in self.active_texts and word not in self.used_words:
                available_words.append(word)
        
        if available_words:
            return self.rng.choice(available_words)
        
        # אם נגמרו המילים הייחודיות, נחזיר מילה אקראית שלא פעילה כרגע
        available_words = []
        for word in self.words_list:
            if word not in self.active_texts:
                available_words.append(word)
        
        if available_words:
            return self.rng.choice(available_words)
        
        # אם גם זה לא עובד, נחזיר מילה אקראית
        return self.rng.choice(self.words_list)

    def spawn_word(self) -> Word:
        text = self._choose_unique_text()
        # עכשיו text לא יכול להיות None כי _choose_unique_text תמיד מחזיר משהו
        wid = f"w_{int(time.time()*1000)}_{self.rng.randrange(100000)}"#זה מבטיח שכל מילה תקבל מזהה שונה לגמרי
        x = float(self.rng.randint(40, CANVAS_W-160))
        y = -40.0
        speed = WORD_SPEED_BASE + self.rng.random()*WORD_SPEED_RAND
        w = Word(id=wid, text=text, x=x, y=y, speed=speed, remaining=text)
        self.words[wid] = w
        self.active_texts.add(text)
        return w

    def _despawn(self, wid: str):
        w = self.words.pop(wid, None)
        if w: 
            self.active_texts.discard(w.text)
            self.used_words.add(w.text)  # הוסף למילים שכבר הופיעו

    def tick(self):
        missed_now = []
        for w in list(self.words.values()):
            if w.status in ("completed","missed"): continue
            w.y += w.speed
            if w.y >= CANVAS_H - 120:
                w.status = "missed"
                missed_now.append(w.id)
                # שחרר את המילה הנוכחית של השחקן אם זו המילה שלו
                for p in self.players.values():
                    if p.current_word_id == w.id:
                        p.current_word_id = None
                    p.lives = max(0, p.lives - MISS_LIFE_PENALTY)
                    p.streak = 0
        if missed_now:
            socketio.emit("word_missed", {"wordIds": missed_now}, room=self.room_id)
            for wid in missed_now: self._despawn(wid)

    def type_char(self, sid: str, ch: str) -> dict:
        with self.lock:
            p = self.players.get(sid)
            if not p or p.lives <= 0:
                return {"type": "noop"}

            ch = ch.lower()
            if len(ch) != 1 or ch not in string.ascii_lowercase:
                return {"type": "noop"}

            # אם יש מילה נעולה לשחקן הזה - המשך רק אותה
            if p.current_word_id and p.current_word_id in self.words:
                w = self.words[p.current_word_id]
                if w.owner_sid == sid and w.remaining and w.remaining[0] == ch:
                    w.typed += ch
                    w.remaining = w.remaining[1:]
                    p.score += SCORE_PER_CHAR
                    p.streak += 1

                    if not w.remaining:
                        w.status = "completed"
                        p.score += WORD_BONUS
                        self._despawn(w.id)
                        p.current_word_id = None  # אפס את המילה הנוכחית
                        return {"type": "completed", "word": w.to_public(self.players),
                                "players": self.public_players(), "completed_by": sid}

                    return {"type": "progress", "word": w.to_public(self.players),
                            "players": self.public_players()}
                else:
                    # אם האות לא מתאימה למילה הנוכחית - זה שגיאה
                    p.streak = 0
                    return {"type": "bad_key"}

            # חיפוש מילה חופשית שמתחילה באות זו - רק אם אין מילה נוכחית
            for w in self.words.values():
                if w.status == "falling" and not w.owner_sid and w.remaining.startswith(ch):
                    w.owner_sid = sid
                    p.current_word_id = w.id
                    w.status = "locked"
                    w.typed = ch
                    w.remaining = w.remaining[1:]
                    p.score += SCORE_PER_CHAR
                    p.streak += 1
                    return {"type": "progress", "word": w.to_public(self.players),
                            "players": self.public_players()}

            # אם אין התאמה
            p.streak = 0
            return {"type": "bad_key"}



    def _all_ready(self):
        """בודק אם שני השחקנים סימנו ready"""
        return len(self.players) == 2 and all(p.ready for p in self.players.values())

    def loop(self):
        """לולאת המשחק — כולל מגבלת זמן של 5 דקות"""
        self.running = True
        interval = 1.0 / TICK_HZ #אומר כל כמה שניות מתבצע “טיק” (כלומר עידכון מצב).
        duration = 300  # 5 דקות
        start_time = time.time()

        try:
            # מחכים לשני השחקנים
            while self.running and not self._all_ready():
                time.sleep(0.05)

            # ספירה לאחור
            for c in [3, 2, 1]:
                socketio.emit("countdown", {"count": c}, room=self.room_id)
                time.sleep(1)

            socketio.emit("game_start", {
                "players": self.public_players(),
                "duration": duration
            }, room=self.room_id)

            # לולאת המשחק
            while self.running and len(self.players) == 2:
                t0 = time.time()

                # בדיקה אם עבר הזמן
                if time.time() - start_time >= duration:
                    self.running = False
                    break

                with self.lock:
                    now = time.time()
                    if now >= self.next_spawn_time:
                        nw = self.spawn_word()  # עכשיו זה תמיד מחזיר מילה
                        socketio.emit("word_spawn", {"words": [nw.to_public(self.players)]}, room=self.room_id)
                        self.next_spawn_time = now + self.rng.uniform(SPAWN_EVERY_MIN, SPAWN_EVERY_MAX)

                    self.tick()
                    # שלח עדכון לכל שחקן בנפרד עם המידע הנכון שלו
                    for sid in self.players.keys():
                        socketio.emit("update_state", {
                            "players": {sid: self.public_players()[sid]},
                            "words": self.snapshot(),
                            "time_left": max(0, int(duration - (time.time() - start_time)))
                        }, room=sid)

                    if all(p.lives <= 0 for p in self.players.values()):
                        self.running = False

                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)#שומר שהלולאה תרוץ בדיוק 30 פעמים בשנייה

        finally:
            # סיום והכרזת מנצח
            scores = [(p.username, p.score) for p in self.players.values()]
            scores.sort(key=lambda x: x[1], reverse=True)
            winner = scores[0] if scores else ("Nobody", 0)
            # שלח עדכון לכל שחקן בנפרד עם המידע הנכון שלו
            for sid in self.players.keys():
                socketio.emit("game_over", {
                    "players": {sid: self.public_players()[sid]},
                    "winner": winner[0],
                    "score": winner[1]
                }, room=sid)

# ====== תור שחקנים ======
WAITING, WAITING_SIDS, ROOM_BY_SID, GAMES = [], set(), {}, {}
PLAYER_COLORS = [(255,120,120),(120,120,255)]

def _pair_if_possible():
    while len(WAITING)>=2:
        sid1,u1 = WAITING.pop(0); WAITING_SIDS.discard(sid1)
        sid2,u2 = WAITING.pop(0); WAITING_SIDS.discard(sid2)
        room=f"room_{int(time.time()*1000)}"
        join_room(room,sid1); join_room(room,sid2)
        ROOM_BY_SID[sid1]=room; ROOM_BY_SID[sid2]=room
        game=Game(room,WORD_BANK)
        game.players[sid1]=Player(sid1,u1,PLAYER_COLORS[0])
        game.players[sid2]=Player(sid2,u2,PLAYER_COLORS[1])
        GAMES[room]=game
        socketio.emit("game_found",{"room":room,"players":{sid1:u1,sid2:u2}},room=room)#נשלחת הודעה ללקוחות (הדפדפנים) ששני שחקנים נמצאו ושיש חדר חדש.
        threading.Thread(target=game.loop,daemon=True).start()#נפתח Thread חדש שמריץ את game.loop() ברקע.

# ====== ROUTES ======
@app.route('/')
def index(): return render_template('exact_game_menu.html')
@app.route('/auth')
def auth_page(): return send_from_directory("templates","auth.html")
@app.route('/online')
def play_online(): return render_template('exact_game.html')

@app.route("/offline")
def offline_game():
    return render_template("offline_game.html")

@app.route("/settings")
def settings():
    return render_template("settings.html")

@app.route("/about")
def about():
    return render_template("about.html")


# ====== API ======
@app.post("/api/signin")
def api_signin():
    data=request.get_json() or {}#שולף את הנתונים שנשלחו ב־POST (בפורמט JSON).
    user=data.get("username","").strip()
    pw=data.get("password","").strip()
    if not os.path.exists("users.json"): return jsonify({"ok":False,"msg":"No users"}),400
    users=json.load(open("users.json","r"))
    if user not in users or users[user]!=pw:
        return jsonify({"ok":False,"msg":"Invalid credentials"}),400
    session["user"]=user#כאן Flask שומר “Session Cookie” בדפדפן של המשתמש.
    return jsonify({"ok":True,"username":user})

@app.post("/api/signup")
def api_signup():
    data=request.get_json() or {}
    email=data.get("username","").strip()
    password=data.get("password","").strip()
    
    if not email or not password:
        return jsonify({"ok":False,"msg":"Email and password required"}),400
    
    if len(password) < 6:
        return jsonify({"ok":False,"msg":"Password must be at least 6 characters"}),400
    
    # Load existing users
    users = {}
    if os.path.exists("users.json"):
        try:
            users = json.load(open("users.json","r"))
        except:
            users = {}
    
    # Check if user already exists
    if email in users:
        return jsonify({"ok":False,"msg":"Email already registered"}),400
    
    # Create new user
    users[email] = password
    
    # Save users
    with open("users.json","w") as f:
        json.dump(users, f, indent=2)
    
    return jsonify({"ok":True,"msg":"Account created successfully!"})

# ====== SOCKET.IO ======
@socketio.on("connect")
def on_connect(): emit("connected",{"sid":request.sid})

@socketio.on("join_game")
def on_join():
    username=session.get("user")
    if not username:
        emit("auth_required",{"msg":"Please sign in first."});return
    sid=request.sid
    if sid in WAITING_SIDS:
        emit("waiting_for_opponent",{"waiting_count":len(WAITING)});return
    WAITING.append((sid,username));WAITING_SIDS.add(sid)
    emit("waiting_for_opponent",{"waiting_count":len(WAITING)})
    _pair_if_possible()#מיד אחרי שהוספנו אותו לתור, נבדוק אם יש עוד שחקן שמחכה.

@socketio.on("client_ready")
def on_client_ready():
    sid=request.sid
    room=ROOM_BY_SID.get(sid)
    if not room:return
    g=GAMES.get(room)
    if not g:return
    with g.lock:
        if sid in g.players:
            g.players[sid].ready=True
            print(f"[READY] {g.players[sid].username} marked ready")

@socketio.on("typed_character")
def on_typed_character(data):
    ch=(data or {}).get("ch","")
    sid=request.sid
    room=ROOM_BY_SID.get(sid)
    if not room:return
    g=GAMES.get(room)
    if not g:return
    result=g.type_char(sid,ch)
    if result["type"]=="progress":
        # שלח עדכון לכל שחקן בנפרד עם המידע הנכון שלו
        for player_sid in g.players.keys():
            socketio.emit("word_update",{"word":result["word"],"players":{player_sid: g.public_players()[player_sid]}},room=player_sid)
    elif result["type"]=="completed":
        # שלח עדכון לכל שחקן בנפרד עם המידע הנכון שלו
        for player_sid in g.players.keys():
            socketio.emit("word_completed",{"word":result["word"],"completed_by":result["completed_by"],
                                            "players":{player_sid: g.public_players()[player_sid]}},room=player_sid)
        try:
            g._despawn(result["word"]["id"])
        except Exception as e:
            print(f"Error despawning word {result['word']['id']}: {e}")
    elif result["type"]=="bad_key":
        emit("bad_key",{},room=sid)

@app.get("/health")
def health():
    return {"ok":True,"waiting":len(WAITING),"games":len(GAMES)}

if __name__=="__main__":
    socketio.run(app,host="0.0.0.0",port=5005,debug=False)
