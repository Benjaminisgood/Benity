from flask import Flask, render_template, request, redirect, session, url_for, jsonify
from neo4j import GraphDatabase, exceptions
import datetime


app = Flask(__name__)
app.secret_key = 'secret_key_for_flask_session'  # Flask会话所需的密钥
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0   # 关闭静态资源缓存（开发期）


# Neo4j 数据库连接配置
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "hhxxhhsh"
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
# 使用方案一：多数据库切换——在此指定当前数据库名
NEO4J_DB = "neo4j"  # 可改为 "campus_dev"、"campus_prod" 等

from neo4j import exceptions

def ensure_database_exists(db_name: str):
    # 先尝试连接目标 DB；不存在就会抛异常
    try:
        with driver.session(database=db_name) as s:
            s.run("RETURN 1").consume()
        return
    except exceptions.Neo4jError as e:
        if getattr(e, "code", "") != "Neo.ClientError.Database.DatabaseNotFound":
            raise  # 不是“数据库不存在”的错误，继续抛出

    # 在 system 数据库里创建，并启动
    with driver.session(database="system") as sys:
        sys.run(f"CREATE DATABASE `{db_name}` IF NOT EXISTS").consume()
        sys.run(f"ALTER DATABASE `{db_name}` START").consume()

    # 可选：初始化约束/索引
    with driver.session(database=db_name) as s:
        s.run("CREATE CONSTRAINT user_id   IF NOT EXISTS FOR (u:User)  REQUIRE u.id IS UNIQUE").consume()
        s.run("CREATE CONSTRAINT event_id  IF NOT EXISTS FOR (e:Event) REQUIRE e.id IS UNIQUE").consume()
        s.run("CREATE CONSTRAINT affair_id IF NOT EXISTS FOR (f:Affair) REQUIRE f.id IS UNIQUE").consume()


# Flask >= 3.0: before_first_request was removed. Initialize DB at import time (idempotent).
ensure_database_exists(NEO4J_DB)

    
def get_new_id(label):
    """获取某标签节点的新ID（当前已有最大ID加1）"""
    with driver.session(database=NEO4J_DB) as session_db:
        result = session_db.run(f"MATCH (n:{label}) RETURN coalesce(max(n.id), 0) AS maxid")
        record = result.single()
        return (record["maxid"] if record and record["maxid"] is not None else 0) + 1

def is_logged_in():
    return 'user_id' in session

@app.route('/')
def index():
    # 未登录则跳转登录页，已登录则跳转主页
    if not is_logged_in():
        return redirect(url_for('login'))
    return redirect(url_for('home'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        sid = request.form.get('student_id')
        key = request.form.get('secret_key')
        with driver.session(database=NEO4J_DB) as session_db:
            result = session_db.run(
                "MATCH (u:User {student_id:$sid, secret_key:$key}) "
                "RETURN u.id AS uid, u.nickname AS nick",
                {'sid': sid, 'key': key})
            record = result.single()
            if record:
                # 登录成功，保存用户会话信息
                session['user_id'] = record['uid']
                session['student_id'] = sid
                session['nickname'] = record['nick']
                return redirect(url_for('home'))
            else:
                # 登录失败，返回登录页并显示错误
                error = "账号或密钥错误"
                return render_template('login.html', error=error)
    else:
        # GET 请求返回登录表单
        return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        sid = request.form.get('student_id')
        gender = request.form.get('gender')
        stage = request.form.get('stage')
        nickname = request.form.get('nickname')
        intro = request.form.get('intro') or ""
        key = request.form.get('secret_key')
        with driver.session(database=NEO4J_DB) as session_db:
            # 检查学号是否已存在
            res = session_db.run("MATCH (u:User {student_id:$sid}) RETURN u", {'sid': sid})
            if res.peek():
                error = "该学号已被注册"
                return render_template('register.html', error=error)
            # 创建新用户节点
            new_id = get_new_id('User')
            session_db.run(
                "CREATE (u:User {id:$id, name:$name, student_id:$sid, gender:$gender, "
                "academic_stage:$stage, nickname:$nick, intro:$intro, secret_key:$key, "
                "friends:$friends, events_created:$evc, events_joined:$evj, "
                "anon_events_created:$aevc, anon_events_joined:$aevj})",
                {
                    'id': new_id, 'name': name, 'sid': sid, 'gender': gender, 'stage': stage,
                    'nick': nickname, 'intro': intro, 'key': key,
                    'friends': [], 'evc': [], 'evj': [], 'aevc': [], 'aevj': []
                }
            )
            # 注册后自动登录
            session['user_id'] = new_id
            session['student_id'] = sid
            session['nickname'] = nickname
            return redirect(url_for('home'))
    else:
        return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/home')
def home():
    if not is_logged_in():
        return redirect(url_for('login'))
    current_user_id = session['user_id']
    nodes = []
    edges = []
    user_map = {}
    with driver.session(database=NEO4J_DB) as session_db:
        # 获取所有用户节点，同时统计likes/dislikes关系数量
        result = session_db.run(
            "MATCH (u:User) "
            "OPTIONAL MATCH (u)<-[:LIKES]-(lu:User) "
            "OPTIONAL MATCH (u)<-[:DISLIKES]-(du:User) "
            "RETURN u.id AS id, u.nickname AS nickname, u.intro AS intro, u.gender AS gender, "
            "count(DISTINCT lu) AS like_count_u, count(DISTINCT du) AS dislike_count_u"
        )
        users = [
            {
                'id': f"u{rec['id']}",
                'raw_id': rec['id'],
                'nickname': rec['nickname'],
                'intro': rec['intro'],
                'gender': rec['gender'],
                'likes': int(rec['like_count_u'] or 0),
                'dislikes': int(rec['dislike_count_u'] or 0),
                'type': "user"
            }
            for rec in result
        ]
        for u in users:
            user_map[u['raw_id']] = u['nickname']
        # 获取所有未截止的活动节点，同时统计likes/dislikes关系数量
        result = session_db.run(
            "MATCH (e:Event) "
            "OPTIONAL MATCH (e)<-[:LIKES]-(lu:User) "
            "OPTIONAL MATCH (e)<-[:DISLIKES]-(du:User) "
            "RETURN e.id AS id, e.name AS name, e.intro AS intro, e.time AS time, e.location AS location, "
            "e.capacity AS capacity, e.current_count AS current_count, e.anon_organizer AS anon_organizer, e.organizer_id AS org_id, e.link AS link, "
            "count(DISTINCT lu) AS like_count_e, count(DISTINCT du) AS dislike_count_e"
        )
        events = []
        for rec in result:
            eid = rec['id']
            # 根据时间判断是否跳过已结束活动
            include = True
            if rec['time']:
                try:
                    ev_time = datetime.datetime.strptime(rec['time'], "%Y-%m-%d %H:%M")
                    if ev_time < datetime.datetime.now():
                        include = False
                except:
                    include = True
            if not include:
                continue
            ev = {
                'id': f"e{eid}",
                'raw_id': eid,
                'name': rec['name'],
                'intro': rec['intro'],
                'time': rec['time'],
                'location': rec['location'],
                'link': rec['link'],
                'capacity': int(rec['capacity']) if rec['capacity'] is not None else 0,
                'current_count': int(rec['current_count']) if rec['current_count'] is not None else 0,
                'anon_organizer': True if rec['org_id'] is not None and rec['anon_organizer'] else False,
                'organizer_id': rec['org_id'] if rec['org_id'] is not None else None,
                'type': "event"
            }
            if ev['organizer_id'] is not None and not ev['anon_organizer']:
                ev['organizer_nickname'] = user_map.get(ev['organizer_id'], "")
            ev['likes'] = int(rec['like_count_e'] or 0)
            ev['dislikes'] = int(rec['dislike_count_e'] or 0)
            events.append(ev)
        # 获取所有事务节点
        result = session_db.run(
                "MATCH (f:Affair) "
                "OPTIONAL MATCH (f)<-[:LIKES]-(lu:User) "
                "OPTIONAL MATCH (f)<-[:DISLIKES]-(du:User) "
                "RETURN f.id AS id, f.content AS content, f.link AS link, f.valid AS valid, "
                "f.comments AS comments, f.poster_id AS poster_id, "
                "count(DISTINCT lu) AS like_count, count(DISTINCT du) AS dislike_count"
)
        affairs = []
        for rec in result:
            fid = rec['id']
            af = {
                'id': f"a{fid}",
                'raw_id': fid,
                'content': rec['content'],
                'link': rec['link'],
                'valid': True if rec['valid'] else False,
                'likes': int(rec['like_count'] or 0),
                'dislikes': int(rec['dislike_count'] or 0),
                'comments': rec['comments'] or [],
                'poster_id': rec['poster_id'],
                'type': "affair"
            }
            affairs.append(af)
        # 汇总所有节点
        nodes = users + events + affairs
        # 建立关系边列表：
        # 1. 好友（互相关注）关系
        result = session_db.run(
            "MATCH (a:User)-[:FOLLOWS]->(b:User) "
            "WHERE (b)-[:FOLLOWS]->(a) AND a.id < b.id "
            "RETURN a.id AS aid, b.id AS bid")
        for rec in result:
            edges.append({'source': f"u{rec['aid']}", 'target': f"u{rec['bid']}"})
        # 2. 当前用户的单向关注
        result = session_db.run(
            "MATCH (c:User {id:$cid})-[:FOLLOWS]->(x:User) "
            "WHERE NOT (x)-[:FOLLOWS]->(c) RETURN x.id AS xid",
            {'cid': current_user_id})
        for rec in result:
            edges.append({'source': f"u{current_user_id}", 'target': f"u{rec['xid']}"})
        # 3. 活动组织关系
        result = session_db.run(
            "MATCH (u:User)-[:ORGANIZES]->(e:Event) RETURN u.id AS uid, e.id AS eid")
        for rec in result:
            edges.append({'source': f"u{rec['uid']}", 'target': f"e{rec['eid']}"})
        # 4. 活动参与关系
        result = session_db.run(
            "MATCH (u:User)-[:PARTICIPATES]->(e:Event) RETURN u.id AS uid, e.id AS eid")
        for rec in result:
            edges.append({'source': f"u{rec['uid']}", 'target': f"e{rec['eid']}"})
        # 5. 事务发布关系
        result = session_db.run(
            "MATCH (u:User)-[:POSTED]->(f:Affair) RETURN u.id AS uid, f.id AS fid")
        for rec in result:
            edges.append({'source': f"u{rec['uid']}", 'target': f"a{rec['fid']}"})
        # 6. 事务提到用户关系
        result = session_db.run(
            "MATCH (f:Affair)-[:MENTIONS]->(u:User) RETURN f.id AS fid, u.id AS uid")
        for rec in result:
            edges.append({'source': f"a{rec['fid']}", 'target': f"u{rec['uid']}"})
        # 7. 事务提到活动关系
        result = session_db.run(
            "MATCH (f:Affair)-[:MENTIONS]->(e:Event) RETURN f.id AS fid, e.id AS eid")
        for rec in result:
            edges.append({'source': f"a{rec['fid']}", 'target': f"e{rec['eid']}"})
        # 计算每个节点的连接数用于调整节点大小
        degree_map = {}
        for edge in edges:
            degree_map[edge['source']] = degree_map.get(edge['source'], 0) + 1
            degree_map[edge['target']] = degree_map.get(edge['target'], 0) + 1
        for node in nodes:
            node_id = node['id']
            node['degree'] = degree_map.get(node_id, 0)
        # 当前用户关注的所有用户列表（用于前端判断是否已关注）
        result = session_db.run(
            "MATCH (c:User {id:$cid})-[:FOLLOWS]->(x:User) RETURN x.id AS xid",
            {'cid': current_user_id})
        following_list = [rec['xid'] for rec in result]
        # 当前用户已加入的活动列表
        result = session_db.run(
            "MATCH (c:User {id:$cid})-[:PARTICIPATES]->(e:Event) RETURN e.id AS eid",
            {'cid': current_user_id})
        joined_events = [rec['eid'] for rec in result]
        # 当前用户匿名加入的活动列表
        result = session_db.run(
            "MATCH (c:User {id:$cid}) RETURN c.anon_events_joined AS anon_joined",
            {'cid': current_user_id})
        rec = result.single()
        anon_joined_events = rec['anon_joined'] if rec and rec['anon_joined'] else []
    focus_target = request.args.get('focus')
    query_back = request.args.get('q')
    return render_template(
        'home.html',
        graph_data={'nodes': nodes, 'edges': edges},
        current_user_id=current_user_id,
        following_list=following_list,
        joined_events=joined_events,
        anon_joined_events=anon_joined_events,
        focus_target=focus_target,
        query_back=query_back
    )

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    if request.method == 'POST':
        name = request.form.get('name')
        gender = request.form.get('gender')
        stage = request.form.get('stage')
        nickname = request.form.get('nickname')
        intro = request.form.get('intro') or ""
        with driver.session(database=NEO4J_DB) as session_db:
            session_db.run(
                "MATCH (u:User {id:$id}) "
                "SET u.name=$name, u.gender=$gender, u.academic_stage=$stage, "
                "u.nickname=$nick, u.intro=$intro",
                {'id': uid, 'name': name, 'gender': gender, 'stage': stage,
                 'nick': nickname, 'intro': intro}
            )
            # 如果需要修改密码，可在此添加类似: 
            # if new_secret: session_db.run("... SET u.secret_key=$key", {...})
        session['nickname'] = nickname
        return redirect(url_for('home'))
    else:
        with driver.session(database=NEO4J_DB) as session_db:
            result = session_db.run(
                "MATCH (u:User {id:$id}) "
                "RETURN u.name AS name, u.student_id AS student_id, "
                "u.gender AS gender, u.academic_stage AS stage, "
                "u.nickname AS nickname, u.intro AS intro",
                {'id': uid})
            rec = result.single()
            if not rec:
                return redirect(url_for('home'))
            user = {
                'name': rec['name'],
                'student_id': rec['student_id'],
                'gender': rec['gender'],
                'academic_stage': rec['stage'],
                'nickname': rec['nickname'],
                'intro': rec['intro']
            }
        return render_template('profile.html', user=user)

@app.route('/event/new', methods=['GET', 'POST'])
def event_form():
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    if request.method == 'POST':
        name = request.form.get('name')
        intro = request.form.get('intro') or ""
        time = request.form.get('time') or ""
        location = request.form.get('location') or ""
        link = request.form.get('link') or ""
        capacity_str = request.form.get('capacity') or "0"
        try:
            capacity = int(capacity_str)
        except:
            capacity = 0
        anon_flag = 'anonymous' in request.form
        with driver.session(database=NEO4J_DB) as session_db:
            eid = get_new_id('Event')
            session_db.run(
                "CREATE (e:Event {id:$id, name:$name, intro:$intro, time:$time, location:$loc, link:$link, "
                "capacity:$cap, current_count:0, anon_organizer:$anon, organizer_id:$orgId})",
                {'id': eid, 'name': name, 'intro': intro, 'time': time, 'loc': location,
                 'link': link, 'cap': capacity, 'anon': True if anon_flag else False, 'orgId': uid}
            )
            if not anon_flag:
                # 非匿名则建立组织关系
                session_db.run(
                    "MATCH (u:User {id:$uid}), (e:Event {id:$eid}) "
                    "CREATE (u)-[:ORGANIZES]->(e)",
                    {'uid': uid, 'eid': eid}
                )
                session_db.run(
                    "MATCH (u:User {id:$uid}) "
                    "SET u.events_created = u.events_created + $eid",
                    {'uid': uid, 'eid': eid}
                )
            else:
                session_db.run(
                    "MATCH (u:User {id:$uid}) "
                    "SET u.anon_events_created = u.anon_events_created + $eid",
                    {'uid': uid, 'eid': eid}
                )
        return redirect(url_for('home', focus=f"e{eid}"))
    else:
        return render_template('event_form.html')

@app.route('/event/edit/<int:eid>', methods=['GET', 'POST'])
def event_edit(eid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        # 检查编辑权限（自己组织的活动才能编辑）
        perm = session_db.run(
            "MATCH (u:User {id:$uid})-[:ORGANIZES]->(e:Event {id:$eid}) RETURN e",
            {'uid': uid, 'eid': eid})
        perm2 = session_db.run(
            "MATCH (e:Event {id:$eid}) RETURN e.anon_organizer AS anon, e.organizer_id AS orgId",
            {'eid': eid})
        allow = False
        rec2 = perm2.single()
        if rec2:
            if (rec2['anon'] and rec2['orgId'] == uid) or (not rec2['anon'] and perm.peek()):
                allow = True
        if not allow:
            return redirect(url_for('home'))
        if request.method == 'POST':
            name = request.form.get('name')
            intro = request.form.get('intro') or ""
            time = request.form.get('time') or ""
            location = request.form.get('location') or ""
            link = request.form.get('link') or ""
            capacity_str = request.form.get('capacity') or "0"
            try:
                capacity = int(capacity_str)
            except:
                capacity = 0
            session_db.run(
                "MATCH (e:Event {id:$eid}) "
                "SET e.name=$name, e.intro=$intro, e.time=$time, e.location=$loc, e.link=$link, e.capacity=$cap",
                {'eid': eid, 'name': name, 'intro': intro, 'time': time,
                 'loc': location, 'link': link, 'cap': capacity}
            )
            return redirect(url_for('home', focus=f"e{eid}"))
        else:
            result = session_db.run(
                "MATCH (e:Event {id:$eid}) "
                "RETURN e.name AS name, e.intro AS intro, e.time AS time, e.location AS location, e.link AS link, "
                "e.capacity AS capacity, e.anon_organizer AS anon, e.organizer_id AS orgId",
                {'eid': eid})
            rec = result.single()
            if not rec:
                return redirect(url_for('home'))
            event = {
                'name': rec['name'],
                'intro': rec['intro'],
                'time': rec['time'],
                'location': rec['location'],
                'link': rec['link'],
                'capacity': rec['capacity'],
                'anon_organizer': True if rec['anon'] else False,
                'organizer_id': rec['orgId']
            }
            organizer_nickname = ""
            if event['organizer_id'] is not None and not event['anon_organizer']:
                res2 = session_db.run(
                    "MATCH (u:User {id:$uid}) RETURN u.nickname AS nick",
                    {'uid': event['organizer_id']})
                r2 = res2.single()
                if r2:
                    organizer_nickname = r2['nick']
            return render_template('event_form.html', event=event, organizer_nickname=organizer_nickname)

@app.route('/affair/new', methods=['GET', 'POST'])
def affair_form():
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    if request.method == 'POST':
        content = request.form.get('content') or ""
        link = request.form.get('link') or ""
        mention_users = request.form.get('mention_users') or ""
        mention_events = request.form.get('mention_events') or ""
        with driver.session(database=NEO4J_DB) as session_db:
            fid = get_new_id('Affair')
            session_db.run(
                "CREATE (f:Affair {id:$id, content:$content, link:$link, valid:true, comments:$comments, poster_id:$poster})",
                {'id': fid, 'content': content, 'link': link, 'comments': [], 'poster': uid}
            )
            session_db.run(
                "MATCH (u:User {id:$uid}), (f:Affair {id:$fid}) "
                "CREATE (u)-[:POSTED]->(f)",
                {'uid': uid, 'fid': fid}
            )
            # 处理提到的用户
            if mention_users:
                ids = [x.strip() for x in mention_users.split(',') if x.strip()]
                for sid in ids:
                    try:
                        sid_num = int(sid)
                    except:
                        continue
                    session_db.run(
                        "MATCH (f:Affair {id:$fid}), (u:User {id:$uid}) "
                        "CREATE (f)-[:MENTIONS]->(u)",
                        {'fid': fid, 'uid': sid_num}
                    )
            # 处理提到的活动
            if mention_events:
                ids = [x.strip() for x in mention_events.split(',') if x.strip()]
                for eid in ids:
                    try:
                        eid_num = int(eid)
                    except:
                        continue
                    session_db.run(
                        "MATCH (f:Affair {id:$fid}), (e:Event {id:$eid}) "
                        "CREATE (f)-[:MENTIONS]->(e)",
                        {'fid': fid, 'eid': eid_num}
                    )
        return redirect(url_for('home', focus=f"a{fid}"))
    else:
        with driver.session(database=NEO4J_DB) as session_db:
            res_users = session_db.run(
                "MATCH (u:User) RETURN u.id AS id, u.nickname AS nickname ORDER BY toLower(u.nickname) ASC"
            )
            all_users = [{'id': r['id'], 'nickname': r['nickname'] or ''} for r in res_users]

            res_events = session_db.run(
                "MATCH (e:Event) RETURN e.id AS id, e.name AS name, e.link AS link ORDER BY toLower(e.name) ASC"
            )
            all_events = [{'id': r['id'], 'name': r['name'] or '', 'link': r['link'] or ''} for r in res_events]

        return render_template('affair_form.html', all_users=all_users, all_events=all_events)

@app.route('/affair/edit/<int:fid>', methods=['GET', 'POST'])
def affair_edit(fid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        # 确认当前用户是该事务的发布者
        perm = session_db.run(
            "MATCH (u:User {id:$uid})-[:POSTED]->(f:Affair {id:$fid}) RETURN f",
            {'uid': uid, 'fid': fid})
        if not perm.peek():
            return redirect(url_for('home'))
        if request.method == 'POST':
            content = request.form.get('content') or ""
            link = request.form.get('link') or ""
            mention_users = request.form.get('mention_users') or ""
            mention_events = request.form.get('mention_events') or ""
            session_db.run(
                "MATCH (f:Affair {id:$fid}) SET f.content=$content, f.link=$link",
                {'fid': fid, 'content': content, 'link': link}
            )
            session_db.run(
                "MATCH (f:Affair {id:$fid})-[r:MENTIONS]->() DELETE r",
                {'fid': fid}
            )
            if mention_users:
                ids = [x.strip() for x in mention_users.split(',') if x.strip()]
                for sid in ids:
                    try:
                        sid_num = int(sid)
                    except:
                        continue
                    session_db.run(
                        "MATCH (f:Affair {id:$fid}), (u:User {id:$uid}) "
                        "CREATE (f)-[:MENTIONS]->(u)",
                        {'fid': fid, 'uid': sid_num}
                    )
            if mention_events:
                ids = [x.strip() for x in mention_events.split(',') if x.strip()]
                for eid in ids:
                    try:
                        eid_num = int(eid)
                    except:
                        continue
                    session_db.run(
                        "MATCH (f:Affair {id:$fid}), (e:Event {id:$eid}) "
                        "CREATE (f)-[:MENTIONS]->(e)",
                        {'fid': fid, 'eid': eid_num}
                    )
            return redirect(url_for('home', focus=f"a{fid}"))
        else:
            result = session_db.run(
                "MATCH (f:Affair {id:$fid}) "
                "OPTIONAL MATCH (f)-[:MENTIONS]->(u:User) "
                "OPTIONAL MATCH (f)-[:MENTIONS]->(e:Event) "
                "RETURN f.content AS content, f.link AS link, "
                "collect(DISTINCT u.id) AS uids, collect(DISTINCT e.id) AS eids",
                {'fid': fid})
            rec = result.single()
            if not rec:
                return redirect(url_for('home'))
            affair = {'content': rec['content'], 'link': rec['link']}
            mentioned_user_ids = [int(x) for x in rec['uids'] if x is not None]
            mentioned_event_ids = [int(x) for x in rec['eids'] if x is not None]

            # Fetch all users' id & nickname for the searchable multi-select
            res_users = session_db.run(
                "MATCH (u:User) RETURN u.id AS id, u.nickname AS nickname ORDER BY toLower(u.nickname) ASC"
            )
            all_users = [{'id': r['id'], 'nickname': r['nickname'] or ''} for r in res_users]

            res_events = session_db.run(
                "MATCH (e:Event) RETURN e.id AS id, e.name AS name, e.link AS link ORDER BY toLower(e.name) ASC"
            )
            all_events = [{'id': r['id'], 'name': r['name'] or '', 'link': r['link'] or ''} for r in res_events]


            return render_template('affair_form.html', affair=affair,
                                   mentioned_user_ids=mentioned_user_ids,
                                   mentioned_event_ids=mentioned_event_ids,
                                   all_users=all_users,
                                   all_events=all_events)

@app.route('/follow/<int:target_id>')
def follow(target_id):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    if target_id == uid:
        return redirect(url_for('home'))
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid}), (t:User {id:$tid}) "
            "MERGE (u)-[:FOLLOWS]->(t)",
            {'uid': uid, 'tid': target_id}
        )
        result = session_db.run(
            "MATCH (t:User {id:$tid})-[:FOLLOWS]->(u:User {id:$uid}) RETURN t",
            {'tid': target_id, 'uid': uid})
        if result.peek():
            session_db.run(
                "MATCH (u:User {id:$uid}), (t:User {id:$tid}) "
                "SET u.friends = CASE WHEN NOT $tid IN u.friends THEN u.friends + $tid ELSE u.friends END, "
                "    t.friends = CASE WHEN NOT $uid IN t.friends THEN t.friends + $uid ELSE t.friends END",
                {'uid': uid, 'tid': target_id}
            )
    return redirect(url_for('home', focus=f"u{target_id}"))

@app.route('/unfollow/<int:target_id>')
def unfollow(target_id):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    if target_id == uid:
        return redirect(url_for('home'))
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid})-[r:FOLLOWS]->(t:User {id:$tid}) DELETE r",
            {'uid': uid, 'tid': target_id}
        )
        result = session_db.run(
            "MATCH (t:User {id:$tid})-[:FOLLOWS]->(u:User {id:$uid}) RETURN t",
            {'tid': target_id, 'uid': uid})
        if result.peek():
            session_db.run(
                "MATCH (u:User {id:$uid}), (t:User {id:$tid}) "
                "SET u.friends = [x IN u.friends WHERE x <> $tid], "
                "    t.friends = [x IN t.friends WHERE x <> $uid]",
                {'uid': uid, 'tid': target_id}
            )
    return redirect(url_for('home', focus=f"u{target_id}"))

@app.route('/join_event/<int:eid>')
def join_event(eid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        result = session_db.run(
            "MATCH (e:Event {id:$eid}) RETURN e.capacity AS cap, e.current_count AS curr",
            {'eid': eid})
        rec = result.single()
        if not rec:
            return redirect(url_for('home'))
        cap = rec['cap'] if rec['cap'] is not None else 0
        curr = rec['curr'] if rec['curr'] is not None else 0
        if cap != 0 and curr >= cap:
            return redirect(url_for('home', focus=f"e{eid}"))
        res2 = session_db.run(
            "MATCH (u:User {id:$uid})-[:PARTICIPATES]->(e:Event {id:$eid}) RETURN e",
            {'uid': uid, 'eid': eid})
        res3 = session_db.run(
            "MATCH (u:User {id:$uid}) RETURN u.events_joined AS evj",
            {'uid': uid})
        existed = res2.peek()
        rec3 = res3.single()
        if rec3 and rec3['evj'] and eid in rec3['evj']:
            existed = True
        if existed:
            return redirect(url_for('home', focus=f"e{eid}"))
        session_db.run(
            "MATCH (u:User {id:$uid}), (e:Event {id:$eid}) "
            "CREATE (u)-[:PARTICIPATES]->(e)",
            {'uid': uid, 'eid': eid}
        )
        session_db.run(
            "MATCH (e:Event {id:$eid}) "
            "SET e.current_count = coalesce(e.current_count,0) + 1",
            {'eid': eid}
        )
        session_db.run(
            "MATCH (u:User {id:$uid}) "
            "SET u.events_joined = u.events_joined + $eid",
            {'uid': uid, 'eid': eid}
        )
    return redirect(url_for('home', focus=f"e{eid}"))

@app.route('/join_event_anon/<int:eid>')
def join_event_anon(eid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        result = session_db.run(
            "MATCH (e:Event {id:$eid}) RETURN e.capacity AS cap, e.current_count AS curr",
            {'eid': eid})
        rec = result.single()
        if not rec:
            return redirect(url_for('home'))
        cap = rec['cap'] if rec['cap'] is not None else 0
        curr = rec['curr'] if rec['curr'] is not None else 0
        if cap != 0 and curr >= cap:
            return redirect(url_for('home', focus=f"e{eid}"))
        res = session_db.run(
            "MATCH (e:Event {id:$eid}) RETURN e.anon_participants AS alist",
            {'eid': eid})
        r = res.single()
        already = False
        if r and r['alist']:
            already = uid in r['alist']
        res2 = session_db.run(
            "MATCH (u:User {id:$uid})-[:PARTICIPATES]->(e:Event {id:$eid}) RETURN e",
            {'uid': uid, 'eid': eid})
        if res2.peek():
            already = True
        if already:
            return redirect(url_for('home', focus=f"e{eid}"))
        session_db.run(
            "MATCH (e:Event {id:$eid}) "
            "SET e.current_count = coalesce(e.current_count,0) + 1, "
            "    e.anon_participants = coalesce(e.anon_participants, []) + $uid",
            {'eid': eid, 'uid': uid}
        )
        session_db.run(
            "MATCH (u:User {id:$uid}) "
            "SET u.anon_events_joined = u.anon_events_joined + $eid",
            {'uid': uid, 'eid': eid}
        )
    return redirect(url_for('home', focus=f"e{eid}"))

@app.route('/leave_event/<int:eid>')
def leave_event(eid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        res = session_db.run(
            "MATCH (u:User {id:$uid})-[r:PARTICIPATES]->(e:Event {id:$eid}) RETURN r",
            {'uid': uid, 'eid': eid})
        normal = res.peek()
        if normal:
            session_db.run(
                "MATCH (u:User {id:$uid})-[r:PARTICIPATES]->(e:Event {id:$eid}) DELETE r",
                {'uid': uid, 'eid': eid}
            )
            session_db.run(
                "MATCH (e:Event {id:$eid}) "
                "SET e.current_count = CASE WHEN coalesce(e.current_count,0) > 0 THEN e.current_count - 1 ELSE 0 END",
                {'eid': eid}
            )
            session_db.run(
                "MATCH (u:User {id:$uid}) "
                "SET u.events_joined = [x IN u.events_joined WHERE x <> $eid]",
                {'uid': uid, 'eid': eid}
            )
        else:
            res2 = session_db.run(
                "MATCH (e:Event {id:$eid}) RETURN e.anon_participants AS alist",
                {'eid': eid})
            r2 = res2.single()
            inAnon = False
            if r2 and r2['alist']:
                inAnon = uid in r2['alist']
            if inAnon:
                session_db.run(
                    "MATCH (e:Event {id:$eid}) "
                    "SET e.anon_participants = [x IN e.anon_participants WHERE x <> $uid], "
                    "    e.current_count = CASE WHEN coalesce(e.current_count,0) > 0 THEN e.current_count - 1 ELSE 0 END",
                    {'eid': eid, 'uid': uid}
                )
                session_db.run(
                    "MATCH (u:User {id:$uid}) "
                    "SET u.anon_events_joined = [x IN u.anon_events_joined WHERE x <> $eid]",
                    {'uid': uid, 'eid': eid}
                )
    return redirect(url_for('home', focus=f"e{eid}"))

@app.route('/like_affair/<int:fid>')
def like_affair(fid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid}), (f:Affair {id:$fid}) "
            "OPTIONAL MATCH (u)-[d:DISLIKES]->(f) DELETE d "
            "WITH u,f "
            "OPTIONAL MATCH (u)-[l:LIKES]->(f) "
            "WITH u,f,l "
            "FOREACH (_ IN CASE WHEN l IS NULL THEN [1] ELSE [] END | CREATE (u)-[:LIKES]->(f)) "
            "FOREACH (_ IN CASE WHEN l IS NOT NULL THEN [1] ELSE [] END | DELETE l)",
            {'uid': uid, 'fid': fid}
        )
    return redirect(url_for('home', focus=f"a{fid}"))

@app.route('/dislike_affair/<int:fid>')
def dislike_affair(fid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid}), (f:Affair {id:$fid}) "
            "OPTIONAL MATCH (u)-[l:LIKES]->(f) DELETE l "
            "WITH u,f "
            "OPTIONAL MATCH (u)-[d:DISLIKES]->(f) "
            "WITH u,f,d "
            "FOREACH (_ IN CASE WHEN d IS NULL THEN [1] ELSE [] END | CREATE (u)-[:DISLIKES]->(f)) "
            "FOREACH (_ IN CASE WHEN d IS NOT NULL THEN [1] ELSE [] END | DELETE d)",
            {'uid': uid, 'fid': fid}
        )
    return redirect(url_for('home', focus=f"a{fid}"))


# Like/dislike routes for Event and User
@app.route('/like_event/<int:eid>')
def like_event(eid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid}), (e:Event {id:$eid}) "
            "OPTIONAL MATCH (u)-[d:DISLIKES]->(e) DELETE d "
            "WITH u,e "
            "OPTIONAL MATCH (u)-[l:LIKES]->(e) "
            "WITH u,e,l "
            "FOREACH (_ IN CASE WHEN l IS NULL THEN [1] ELSE [] END | CREATE (u)-[:LIKES]->(e)) "
            "FOREACH (_ IN CASE WHEN l IS NOT NULL THEN [1] ELSE [] END | DELETE l)",
            {'uid': uid, 'eid': eid}
        )
    return redirect(url_for('home', focus=f"e{eid}"))

@app.route('/dislike_event/<int:eid>')
def dislike_event(eid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid}), (e:Event {id:$eid}) "
            "OPTIONAL MATCH (u)-[l:LIKES]->(e) DELETE l "
            "WITH u,e "
            "OPTIONAL MATCH (u)-[d:DISLIKES]->(e) "
            "WITH u,e,d "
            "FOREACH (_ IN CASE WHEN d IS NULL THEN [1] ELSE [] END | CREATE (u)-[:DISLIKES]->(e)) "
            "FOREACH (_ IN CASE WHEN d IS NOT NULL THEN [1] ELSE [] END | DELETE d)",
            {'uid': uid, 'eid': eid}
        )
    return redirect(url_for('home', focus=f"e{eid}"))

@app.route('/like_user/<int:target_id>')
def like_user(target_id):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    if uid == target_id:
        return redirect(url_for('home', focus=f"u{target_id}"))
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid}), (t:User {id:$tid}) "
            "OPTIONAL MATCH (u)-[d:DISLIKES]->(t) DELETE d "
            "WITH u,t "
            "OPTIONAL MATCH (u)-[l:LIKES]->(t) "
            "WITH u,t,l "
            "FOREACH (_ IN CASE WHEN l IS NULL THEN [1] ELSE [] END | CREATE (u)-[:LIKES]->(t)) "
            "FOREACH (_ IN CASE WHEN l IS NOT NULL THEN [1] ELSE [] END | DELETE l)",
            {'uid': uid, 'tid': target_id}
        )
    return redirect(url_for('home', focus=f"u{target_id}"))

@app.route('/dislike_user/<int:target_id>')
def dislike_user(target_id):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    if uid == target_id:
        return redirect(url_for('home', focus=f"u{target_id}"))
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (u:User {id:$uid}), (t:User {id:$tid}) "
            "OPTIONAL MATCH (u)-[l:LIKES]->(t) DELETE l "
            "WITH u,t "
            "OPTIONAL MATCH (u)-[d:DISLIKES]->(t) "
            "WITH u,t,d "
            "FOREACH (_ IN CASE WHEN d IS NULL THEN [1] ELSE [] END | CREATE (u)-[:DISLIKES]->(t)) "
            "FOREACH (_ IN CASE WHEN d IS NOT NULL THEN [1] ELSE [] END | DELETE d)",
            {'uid': uid, 'tid': target_id}
        )
    return redirect(url_for('home', focus=f"u{target_id}"))


@app.route('/add_comment/<int:fid>', methods=['POST'])
def add_comment(fid):
    if not is_logged_in():
        return redirect(url_for('login'))
    comment = request.form.get('comment') or ""
    if comment.strip() == "":
        return redirect(url_for('home', focus=f"a{fid}"))
    with driver.session(database=NEO4J_DB) as session_db:
        session_db.run(
            "MATCH (f:Affair {id:$fid}) SET f.comments = coalesce(f.comments, []) + $cmt",
            {'fid': fid, 'cmt': comment}
        )
    return redirect(url_for('home', focus=f"a{fid}"))

@app.route('/toggle_affair/<int:fid>')
def toggle_affair(fid):
    if not is_logged_in():
        return redirect(url_for('login'))
    uid = session['user_id']
    with driver.session(database=NEO4J_DB) as session_db:
        res = session_db.run(
            "MATCH (u:User {id:$uid})-[:POSTED]->(f:Affair {id:$fid}) RETURN f.valid AS valid",
            {'uid': uid, 'fid': fid})
        rec = res.single()
        if not rec:
            return redirect(url_for('home'))
        current_valid = rec['valid']
        new_valid = False if current_valid else True
        session_db.run(
            "MATCH (f:Affair {id:$fid}) SET f.valid = $newval",
            {'fid': fid, 'newval': new_valid}
        )
    return redirect(url_for('home', focus=f"a{fid}"))

@app.route('/search')
def search():
    query = request.args.get('q', '')
    users = []; events = []; affairs = []
    if query:
        with driver.session(database=NEO4J_DB) as session_db:
            res_u = session_db.run(
                "MATCH (u:User) WHERE u.nickname CONTAINS $q OR u.intro CONTAINS $q "
                "RETURN u.id AS id, u.nickname AS nickname",
                {'q': query})
            users = [{'id': rec['id'], 'nickname': rec['nickname']} for rec in res_u]
            res_e = session_db.run(
                "MATCH (e:Event) WHERE e.name CONTAINS $q OR e.intro CONTAINS $q "
                "RETURN e.id AS id, e.name AS name",
                {'q': query})
            events = [{'id': rec['id'], 'name': rec['name']} for rec in res_e]
            res_f = session_db.run(
                "MATCH (f:Affair) WHERE f.content CONTAINS $q "
                "RETURN f.id AS id, f.content AS content",
                {'q': query})
            affairs = [{'id': rec['id'], 'content': rec['content']} for rec in res_f]
    return render_template('search.html', query=query, users=users, events=events, affairs=affairs)





@app.get('/api/users')
def api_users():
    if not is_logged_in():
        return jsonify([])
    with driver.session(database=NEO4J_DB) as s:
        res = s.run("MATCH (u:User) RETURN u.id AS id, u.nickname AS nickname ORDER BY toLower(u.nickname)")
        return jsonify([{'id': r['id'], 'nickname': r['nickname'] or ''} for r in res])

@app.get('/api/events')
def api_events():
    if not is_logged_in():
        return jsonify([])
    with driver.session(database=NEO4J_DB) as s:
        res = s.run("MATCH (e:Event) RETURN e.id AS id, e.name AS name, e.link AS link ORDER BY toLower(e.name)")
        return jsonify([{'id': r['id'], 'name': r['name'] or '', 'link': r['link'] or ''} for r in res])
    



    
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5005, debug=True)