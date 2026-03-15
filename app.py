import os
import json
import requests
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-skywiki')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///skywiki.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# OpenRouter
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', 'nvidia/llama-nemotron-embed-vl-1b-v2:free')
MODERATION_MODEL = os.environ.get('MODERATION_MODEL', OPENROUTER_MODEL)

# ================== МОДЕЛИ ==================
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    articles = db.relationship('Article', backref='author', lazy=True)

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
    return User.query.get(int(user_id))

# Создание таблиц
with app.app_context():
    db.create_all()
    if User.query.count() == 0:
        admin = User(
            username='admin',
            email='admin@skywiki.com',
            password_hash=generate_password_hash('admin123'),
            is_admin=True
        )
        db.session.add(admin)
        db.session.commit()

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

        # Логируем
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
        print(f"Moderation error: {e}")
        return True

# ================== МАРШРУТЫ ==================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/ping', methods=['GET'])
def ping():
    return 'pong', 200

# API: Статьи
@app.route('/api/articles', methods=['GET'])
def get_articles():
    articles = Article.query.filter_by(is_hidden=False).order_by(Article.created_at.desc()).all()
    return jsonify([{
        'id': a.id,
        'title': a.title,
        'created_at': a.created_at.isoformat(),
        'views': a.views,
        'author': a.author.username
    } for a in articles])

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
    if not data.get('title') or not data.get('content'):
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

# API: Пользователи
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username exists'}), 400
    user = User(
        username=data['username'],
        email=data['email'],
        password_hash=generate_password_hash(data['password'])
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'message': 'User created'}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data['username']).first()
    if user and check_password_hash(user.password_hash, data['password']):
        login_user(user)
        return jsonify({
            'message': 'Logged in',
            'user': {'id': user.id, 'username': user.username, 'is_admin': user.is_admin}
        })
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({'message': 'Logged out'})

@app.route('/api/me', methods=['GET'])
@login_required
def me():
    return jsonify({
        'id': current_user.id,
        'username': current_user.username,
        'email': current_user.email,
        'is_admin': current_user.is_admin
    })

# API: AI генерация
@app.route('/api/ai/generate', methods=['POST'])
@login_required
def ai_generate():
    data = request.json
    prompt = data.get('prompt')
    if not prompt:
        return jsonify({'error': 'Prompt required'}), 400
    if not OPENROUTER_API_KEY:
        return jsonify({'error': 'AI service not configured'}), 503

    try:
        response = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
                'HTTP-Referer': request.host_url,
                'X-Title': 'SkyWiki'
            },
            json={
                'model': OPENROUTER_MODEL,
                'messages': [
                    {'role': 'system', 'content': 'Ты — помощник вики-энциклопедии SkyWiki. Создавай статьи в формате вики-текста (== заголовки ==, * списки, **жирный**).'},
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': 1500,
                'temperature': 0.7
            },
            timeout=30
        )
        data = response.json()
        if 'choices' not in data:
            return jsonify({'error': 'AI service error'}), 502
        return jsonify({'response': data['choices'][0]['message']['content']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
