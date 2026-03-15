import os
import json
import logging
import traceback
import requests
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask_cors import CORS

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-skywiki')

# База данных SQLite
db_path = os.environ.get('DB_PATH', 'skywiki.db')
if not db_path.startswith('/'):
    db_path = os.path.join(os.path.dirname(__file__), db_path)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# OpenRouter (для модерации)
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', 'meta-llama/llama-3.2-3b-instruct:free')
MODERATION_MODEL = os.environ.get('MODERATION_MODEL', OPENROUTER_MODEL)

# ================== МОДЕЛИ ==================
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    articles = db.relationship('Article', backref='author', lazy=True)

    @property
    def is_active(self):
        return True

class Article(db.Model):
    __tablename__ = 'articles'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    views = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_hidden = db.Column(db.Boolean, default=False)

class ModerationLog(db.Model):
    __tablename__ = 'moderation_logs'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    content = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    verdict = db.Column(db.String(10))
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Создание таблиц и админа при первом запуске
with app.app_context():
    db.create_all()
    if User.query.count() == 0:
        admin = User(
            username='admin',
            email='admin@skywiki.com',
            password_hash=generate_password_hash('DeBard000Aeerg+=r'),
            is_admin=True
        )
        db.session.add(admin)
        db.session.commit()
        logger.info("Admin user created (admin/admin123)")

# ================== МОДЕРАЦИЯ ==================
def moderate_content(title, content):
    if not OPENROUTER_API_KEY:
        return True
    full_text = f"Заголовок: {title}\n\nТекст статьи:\n{content[:2000]}"
    try:
        response = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
                'HTTP-Referer': request.host_url,
                'X-Title': 'SkyWiki Moderator'
            },
            json={
                'model': MODERATION_MODEL,
                'messages': [
                    {'role': 'system', 'content': 'Ты — модератор энциклопедии SkyWiki. Оцени статью по трём критериям: 1) отсутствие рекламы, 2) отсутствие оскорблений и нецензурной лексики, 3) наличие смысла. Ответь ТОЛЬКО в формате JSON: {"verdict": "OK"} или {"verdict": "NOT OK"}. Никаких других слов.'},
                    {'role': 'user', 'content': full_text}
                ],
                'max_tokens': 50,
                'temperature': 0.1
            },
            timeout=15
        )
        data = response.json()
        if 'choices' not in data:
            return True
        answer = data['choices'][0]['message']['content']
        try:
            verdict_data = json.loads(answer)
            verdict = verdict_data.get('verdict') == 'OK'
        except:
            verdict = 'OK' in answer and 'NOT OK' not in answer
        if current_user.is_authenticated:
            log = ModerationLog(
                title=title[:200],
                content=content[:500],
                user_id=current_user.id,
                verdict='OK' if verdict else 'NOT OK',
                reason=answer[:200]
            )
            db.session.add(log)
            db.session.commit()
        return verdict
    except Exception as e:
        logger.error(f"Moderation error: {e}")
        return True

# ================== МАРШРУТЫ ==================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/ping')
def ping():
    return 'pong'

# API: Статьи
@app.route('/api/articles', methods=['GET'])
def get_articles():
    arts = Article.query.filter_by(is_hidden=False).order_by(Article.created_at.desc()).all()
    return jsonify([{
        'id': a.id,
        'title': a.title,
        'created_at': a.created_at.isoformat(),
        'views': a.views,
        'author': a.author.username
    } for a in arts])

@app.route('/api/articles/<int:article_id>', methods=['GET'])
def get_article(article_id):
    article = Article.query.get_or_404(article_id)
    article.views += 1
    db.session.commit()
    return jsonify({
        'id': article.id,
        'title': article.title,
        'content': article.content,
        'created_at': article.created_at.isoformat(),
        'updated_at': article.updated_at.isoformat(),
        'views': article.views,
        'author': article.author.username,
        'can_edit': current_user.is_authenticated and (current_user.id == article.user_id or current_user.is_admin)
    })

@app.route('/api/articles', methods=['POST'])
@login_required
def create_article():
    data = request.json
    if not data or not data.get('title') or not data.get('content'):
        return jsonify({'error': 'Title and content required'}), 400
    if not moderate_content(data['title'], data['content']):
        return jsonify({'error': 'Статья не прошла модерацию. Проверьте, нет ли в ней рекламы, оскорблений или бессмыслицы.'}), 400
    article = Article(
        title=data['title'],
        content=data['content'],
        user_id=current_user.id
    )
    db.session.add(article)
    db.session.commit()
    return jsonify({'id': article.id, 'message': 'Article created'}), 201

@app.route('/api/articles/<int:article_id>', methods=['PUT'])
@login_required
def update_article(article_id):
    article = Article.query.get_or_404(article_id)
    if not (current_user.id == article.user_id or current_user.is_admin):
        return jsonify({'error': 'Permission denied'}), 403
    data = request.json
    new_title = data.get('title', article.title)
    new_content = data.get('content', article.content)
    if not moderate_content(new_title, new_content):
        return jsonify({'error': 'Обновлённая статья не прошла модерацию.'}), 400
    article.title = new_title
    article.content = new_content
    db.session.commit()
    return jsonify({'message': 'Article updated'})

@app.route('/api/articles/<int:article_id>', methods=['DELETE'])
@login_required
def delete_article(article_id):
    article = Article.query.get_or_404(article_id)
    if not (current_user.id == article.user_id or current_user.is_admin):
        return jsonify({'error': 'Permission denied'}), 403
    article.is_hidden = True
    db.session.commit()
    return jsonify({'message': 'Article deleted'})

# ================== АВТОРИЗАЦИЯ ==================
@app.route('/api/register', methods=['POST'])
def register():
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No JSON data'}), 400
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        if not username or not email or not password:
            return jsonify({'error': 'Missing fields'}), 400
        if User.query.filter_by(username=username).first():
            return jsonify({'error': 'Username exists'}), 400
        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password)
        )
        db.session.add(user)
        db.session.commit()
        logger.info(f"New user registered: {username}")
        return jsonify({'message': 'User created'}), 201
    except Exception as e:
        logger.error(f"Register error: {e}\n{traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return jsonify({'error': 'Method GET not allowed. Please send POST with username and password.'}), 405

    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No JSON data'}), 400
        username = data.get('username')
        password = data.get('password')
        if not username or not password:
            return jsonify({'error': 'Missing credentials'}), 400

        user = User.query.filter_by(username=username).first()
        if not user:
            logger.warning(f"Login attempt with unknown user: {username}")
            return jsonify({'error': 'Invalid credentials'}), 401

        if not check_password_hash(user.password_hash, password):
            logger.warning(f"Wrong password for user: {username}")
            return jsonify({'error': 'Invalid credentials'}), 401

        login_user(user)
        logger.info(f"User logged in: {username}")
        return jsonify({
            'message': 'Logged in',
            'user': {
                'id': user.id,
                'username': user.username,
                'is_admin': user.is_admin
            }
        })
    except Exception as e:
        logger.error(f"Login error: {e}\n{traceback.format_exc()}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    try:
        logout_user()
        return jsonify({'message': 'Logged out'})
    except Exception as e:
        logger.error(f"Logout error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/me', methods=['GET'])
@login_required
def me():
    return jsonify({
        'id': current_user.id,
        'username': current_user.username,
        'email': current_user.email,
        'is_admin': current_user.is_admin
    })

# ================== АДМИН-ПАНЕЛЬ ==================
@app.route('/admin')
@login_required
def admin_panel():
    if not current_user.is_admin:
        return "Access denied", 403
    return render_template('admin.html')

@app.route('/api/admin/users', methods=['GET'])
@login_required
def admin_get_users():
    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403
    users = User.query.all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'email': u.email,
        'is_admin': u.is_admin,
        'created_at': u.created_at.isoformat(),
        'articles_count': len(u.articles)
    } for u in users])

@app.route('/api/admin/articles', methods=['GET'])
@login_required
def admin_get_articles():
    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403
    arts = Article.query.order_by(Article.created_at.desc()).all()
    return jsonify([{
        'id': a.id,
        'title': a.title,
        'author': a.author.username,
        'created_at': a.created_at.isoformat(),
        'views': a.views,
        'is_hidden': a.is_hidden
    } for a in arts])

@app.route('/api/admin/articles/<int:article_id>/toggle', methods=['POST'])
@login_required
def admin_toggle_article(article_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403
    article = Article.query.get_or_404(article_id)
    article.is_hidden = not article.is_hidden
    db.session.commit()
    return jsonify({'message': 'Toggled', 'is_hidden': article.is_hidden})

@app.route('/api/admin/moderation-logs', methods=['GET'])
@login_required
def admin_moderation_logs():
    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403
    logs = ModerationLog.query.order_by(ModerationLog.created_at.desc()).limit(100).all()
    return jsonify([{
        'id': l.id,
        'title': l.title,
        'verdict': l.verdict,
        'reason': l.reason,
        'created_at': l.created_at.isoformat(),
        'user_id': l.user_id
    } for l in logs])

# ================== AI ГЕНЕРАЦИЯ (ОТКЛЮЧЕНА) ==================
@app.route('/api/ai/generate', methods=['POST'])
@login_required
def ai_generate():
    return jsonify({'error': 'AI generation is currently disabled'}), 503

# ================== HEALTH CHECK ==================
@app.route('/api/health', methods=['GET'])
def health():
    try:
        users_count = User.query.count()
        return jsonify({'status': 'ok', 'users': users_count})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
