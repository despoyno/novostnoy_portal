from flask import Flask, render_template, request, redirect, session, g, url_for, flash
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from bs4 import BeautifulSoup
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import os
import time
import logging
import uuid
from g4f.client import Client
# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def remove_html_tags(text):
    """
    Удаляет все HTML/XML-теги из текста.
    :param text: Исходный текст с тегами.
    :return: Текст без тегов.
    """
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(separator="\n")

# Проверка существования файла перед записью
if not os.path.exists("notifications.txt"):
    open("notifications.txt", "w").close()  # Создаем пустой файл

# Настройка Flask-приложения
app = Flask(__name__)
app.secret_key = os.urandom(24)
DATABASE = 'users.db'

# Функции для работы с базой данных
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# Инициализация базы данных
def init_db():
    with app.app_context():
        db = get_db()
        # Таблица пользователей
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                unique_code TEXT UNIQUE,
                telegram_chat_id INTEGER,
                fio TEXT,                  -- Новое поле для ФИО
                email TEXT UNIQUE,         -- Новое поле для email
                data_reg TIMESTAMP DEFAULT CURRENT_TIMESTAMP -- Новое поле для даты регистрации
            )
        ''')
        
        # Таблица новостей
        db.execute('''
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                source_id INTEGER,
                published_at TIMESTAMP,
                moderated BOOLEAN DEFAULT 0,
                user_id INTEGER,
                link TEXT UNIQUE,
                category INTEGER
            )
        ''')
        
        # Таблица источников
        db.execute('''
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL
            )
        ''')
        
        # Таблица отправленных уведомлений
        db.execute('''
            CREATE TABLE IF NOT EXISTS sent_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER UNIQUE NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Добавление тестового источника
        db.execute('INSERT OR IGNORE INTO sources (name, url) VALUES (?, ?)',
                   ('РНФ', 'https://rscf.ru/news/rss/'))
        
        # Добавление тестового пользователя
        db.execute('''
            INSERT OR IGNORE INTO users (username, password_hash, role, unique_code)
            VALUES (?, ?, "admin", ?)
        ''', ("admin", generate_password_hash("admin123"), str(uuid.uuid4())[:8]))
        db.execute('''
            CREATE TABLE IF NOT EXISTS likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                liked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(news_id, user_id)
            )
        ''')
        
        db.commit()

# Декоратор для проверки ролей
def check_role(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                return redirect('/login')
            db = get_db()
            user = db.execute('SELECT role FROM users WHERE username = ?', (session['user'],)).fetchone()
            if user['role'] != role:
                return "Доступ запрещён", 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Парсинг RSS-каналов с повторными попытками
def fetch_with_retry(url, retries=5, backoff_factor=0.3):
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry

    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    try:
        response = session.get(url, timeout=10)
        response.raise_for_status()
        return response.content
    except Exception as e:
        logging.error(f"Ошибка при парсинге {url}: {e}")
        return None

def format_date(pub_date):
    """
    Преобразует дату из формата RSS в формат "ДД.ММ.ГГГГ ЧЧ.ММ".
    :param pub_date: Строка даты из RSS-ленты.
    :return: Отформатированная строка даты.
    """
    if not pub_date:
        return "Дата не указана"
    try:
        # Парсим дату из RSS-формата
        date_obj = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z")
        # Преобразуем в желаемый формат
        return date_obj.strftime("%d.%m.%Y %H.%M")
    except ValueError as e:
        logging.error(f"Ошибка при парсинге даты '{pub_date}': {e}")
        return "Неверный формат даты"


def parse_rss(url):
    content = fetch_with_retry(url)
    if not content:
        return []
    soup = BeautifulSoup(content, 'xml')
    items = soup.find_all('item')
    news_list = []
    for item in items:
        # Извлечение заголовка
        title = item.find('title').text if item.find('title') else "Без заголовка"

        # Извлечение ссылки
        link = item.find('link').text if item.find('link') else "Без ссылки"

        # Извлечение описания
        description_tag = item.find('description')
        yandex_full_text_tag = item.find('yandex:full-text')
        if description_tag and description_tag.text.strip():
            # Удаляем HTML/XML-теги из description
            soup_desc = BeautifulSoup(description_tag.text.strip(), "html.parser")
            description = soup_desc.get_text(separator="\n").strip()
        elif yandex_full_text_tag and yandex_full_text_tag.text.strip():
            # Удаляем HTML/XML-теги из yandex:full-text
            soup_desc = BeautifulSoup(yandex_full_text_tag.text.strip(), "html.parser")
            description = soup_desc.get_text(separator="\n").strip()
        else:
            description = "Без описания"

        # Извлечение даты публикации и форматирование
        pub_date = item.find('pubDate').text if item.find('pubDate') else None
        formatted_date = format_date(pub_date)  # Преобразуем дату

        # Добавление новости в список
        news_list.append({
            'title': title,
            'link': link,
            'description': description,
            'pub_date': formatted_date  # Сохраняем отформатированную дату
        })
    return news_list


# Определение категории новости
def determine_category(title):
    client = Client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Определи категорию: {title}. Ответ — число: 1 - медицина, 2 - политика, 3 - учёба, 4 - спорт, 5 - другое. Только число."}],
        web_search=False
    )
    category_code = response.choices[0].message.content.strip()
    print(f"Ответ API для '{title}': {category_code}")  # Отладочный вывод
    try:
        return int(category_code)
    except ValueError:
        logging.error(f"Ошибка категории для '{title}': '{category_code}'")
        return 5
# Обновление новостей из источников с немедленным добавлением в БД
def fetch_news_from_sources():
    start_time = time.time()
    logging.info("Запуск задачи fetch_news_from_sources...")
    
    with app.app_context():
        db = get_db()
        sources = db.execute('SELECT id, url FROM sources').fetchall()
        
        for source in sources:
            source_id = source['id']
            url = source['url']
            logging.info(f"Парсинг источника: {url}")
            
            # Парсим RSS-ленту
            news_list = parse_rss(url)
            
            # Обрабатываем каждую новость
            for news in news_list:
                try:
                    # Проверяем, существует ли новость в базе данных
                    existing = db.execute('SELECT id FROM news WHERE link = ?', (news['link'],)).fetchone()
                    if not existing:
                        # Определяем категорию новости
                        category = determine_category(news['title'])
                        # Добавляем новость в базу данных
                        db.execute('''
                            INSERT INTO news (title, content, source_id, published_at, link, moderated, category)
                            VALUES (?, ?, ?, ?, ?, 1, ?)
                        ''', (news['title'], news['description'], source_id, news['pub_date'], news['link'], category))
                        
                        # Фиксируем изменения в базе данных
                        db.commit()
                        
                        logging.info(f"Новость добавлена: {news['title']}")
                    else:
                        logging.info(f"Новость уже существует: {news['title']}")
                except Exception as e:
                    logging.error(f"Ошибка при добавлении новости '{news['title']}': {e}")
    
    logging.info(f"Задача завершена за {time.time() - start_time:.2f} секунд")
# Главная страница
@app.route('/')
def index():
    if 'user' in session:
        return redirect('/news')
    return redirect('/login')

# Вход
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if not username or not password:
            flash('Заполните все поля!', 'danger')
            return render_template('login.html')
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['user'] = username
            session['user_id'] = user['id']
            flash('Вы успешно вошли!', 'success')
            return redirect('/')
        else:
            flash('Неверный логин или пароль', 'danger')
    return render_template('login.html')

# Регистрация
@app.route('/profile')
def profile():
    if 'user' not in session:
        flash('Для просмотра профиля необходимо войти.', 'danger')
        return redirect('/login')

    db = get_db()
    user = db.execute('''
        SELECT username, fio, email, data_reg 
        FROM users 
        WHERE id = ?
    ''', (session['user_id'],)).fetchone()

    if not user:
        flash('Пользователь не найден.', 'danger')
        return redirect('/login')

    # Получаем лайкнутые новости
    liked_news = db.execute('''
        SELECT n.id, n.title, n.published_at, n.link  -- Добавлено поле link
        FROM news n
        JOIN likes l ON n.id = l.news_id
        WHERE l.user_id = ?
    ''', (session['user_id'],)).fetchall()

    return render_template('profile.html', 
                           username=user['username'], 
                           full_name=user['fio'], 
                           email=user['email'], 
                           registration_date=user['data_reg'],
                           liked_news=liked_news)
# Страница новостей
@app.route('/news', methods=['GET'])
def news():
    if 'user' not in session:
        return redirect('/login')

    # Получаем параметры фильтрации
    filter_type = request.args.get('filter', 'all')
    category = request.args.get('category', 'all')
    db = get_db()

    query = '''
        SELECT 
            news.id, 
            news.title, 
            news.content, 
            news.published_at, 
            news.link, 
            news.category,
            COUNT(likes.id) AS like_count,
            MAX(CASE WHEN likes.user_id = ? THEN 1 ELSE 0 END) AS is_liked
        FROM news
        LEFT JOIN likes ON news.id = likes.news_id
        WHERE news.moderated = 1
    '''
    params = [session['user_id']]

    # Фильтрация по времени
    if filter_type == 'today':
        query += " AND DATE(news.published_at) = DATE('now')"
    elif filter_type == 'week':
        query += " AND news.published_at >= DATE('now', '-7 days')"
    elif filter_type == 'month':
        query += " AND news.published_at >= DATE('now', '-30 days')"

    # Фильтрация по категории
    if category != 'all':
        try:
            category_int = int(category)
            query += " AND news.category = ?"
            params.append(category_int)
        except ValueError:
            pass

    query += " GROUP BY news.id ORDER BY news.published_at DESC"

    # Выполняем запрос с фильтрами
    news_items = db.execute(query, params).fetchall()
    for item in news_items:
        print(f"Новость: {item['title']}, Категория: {item['category']}")

    return render_template('news.html', news=news_items, filter=filter_type, category=category)
# Админ-панель
@app.route('/admin', methods=['GET', 'POST'])
@check_role('admin')
def admin_panel():
    db = get_db()
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        category = request.form['category']
        try:
            db.execute('''
                INSERT INTO news (title, content, category, moderated, user_id)
                VALUES (?, ?, ?, 1, ?)
            ''', (title, content, category, session.get('user_id')))
            db.commit()
            flash('Новость добавлена!', 'success')
        except Exception as e:
            flash(f'Ошибка: {e}', 'error')
        return redirect('/admin')
    pending_news = db.execute('''
        SELECT news.*, users.username 
        FROM news 
        LEFT JOIN users ON news.user_id = users.id 
        WHERE moderated = 0
    ''').fetchall()
    users = db.execute('SELECT id, username, role FROM users').fetchall()
    return render_template('admin.html', pending_news=pending_news, users=users, param = True)

# Одобрение новости
@app.route('/approve-news/<int:news_id>', methods=['POST'])
@check_role('admin')
def approve_news(news_id):
    db = get_db()
    try:
        db.execute('UPDATE news SET moderated = 1 WHERE id = ?', (news_id,))
        db.commit()
        flash('Новость одобрена!', 'success')
    except Exception as e:
        flash(f'Ошибка при одобрении новости: {e}', 'error')
    return redirect('/admin')

# Отклонение новости
@app.route('/reject-news/<int:news_id>', methods=['POST'])
@check_role('admin')
def reject_news(news_id):
    db = get_db()
    try:
        db.execute('DELETE FROM news WHERE id = ?', (news_id,))
        db.commit()
        flash('Новость отклонена и удалена!', 'success')
    except Exception as e:
        flash(f'Ошибка при отклонении новости: {e}', 'error')
    return redirect('/admin')

@app.route('/add_news', methods=['GET'])
def add_news_form():
    if 'user' not in session:
        return redirect('/login')
    return render_template('add_news.html')

@app.route('/add_news', methods=['POST'])
def add_news():
    if 'user' not in session:
        return redirect('/login')
    
    # Получаем данные из формы
    title = request.form.get('title')
    content = request.form.get('content')
    category = request.form.get('category')

    # Проверяем, что все поля заполнены
    if not title or not content or not category:
        flash('Заполните все поля!', 'danger')
        return redirect('/add_news')

    try:
        # Преобразуем категорию в целое число
        category_int = int(category)
    except ValueError:
        flash('Категория должна быть числом!', 'danger')
        return redirect('/add_news')

    db = get_db()
    if not db:
        flash('Ошибка подключения к базе данных!', 'error')
        return redirect('/add_news')

    try:
        # Добавляем новость в базу данных с флагом moderated = 0 (на модерации)
        db.execute('''
            INSERT INTO news (title, content, category, moderated, user_id)
            VALUES (?, ?, ?, 0, ?)
        ''', (title, content, category_int, session.get('user_id')))
        db.commit()
        flash('Новость отправлена на модерацию!', 'success')
    except Exception as e:
        print(f"Ошибка при выполнении SQL-запроса: {e}")
        flash(f'Ошибка при добавлении новости: {e}', 'error')
    
    return redirect('/news')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # Получаем данные из формы
        username = request.form.get('username')
        password = request.form.get('password')
        full_name = request.form.get('full_name')  # Новое поле ФИО
        email = request.form.get('email')          # Новое поле email

        # Проверяем, что все поля заполнены
        if not all([username, password, full_name, email]):
            flash('Заполните все поля!', 'danger')
            return render_template('register.html')

        # Хэшируем пароль
        password_hash = generate_password_hash(password)

        # Генерируем уникальный код
        unique_code = str(uuid.uuid4())[:8]

        # Подключаемся к базе данных
        db = get_db()

        try:
            # Добавляем пользователя в базу данных
            db.execute('''
                INSERT INTO users (username, password_hash, role, unique_code, fio, email)
                VALUES (?, ?, "user", ?, ?, ?)
            ''', (username, password_hash, unique_code, full_name, email))
            db.commit()
            flash(f"Регистрация успешна! Ваш код для Telegram: {unique_code}", "success")
            return redirect('/login')
        except sqlite3.IntegrityError:
            flash('Пользователь с таким email или именем уже существует', 'danger')
        except Exception as e:
            logging.error(f"Ошибка при регистрации: {e}")
            flash('Произошла ошибка при регистрации. Попробуйте снова.', 'danger')

    return render_template('register.html')

@app.route('/like/<int:news_id>', methods=['POST'])
def like_news(news_id):
    if 'user' not in session:
        return {"error": "Для лайков необходимо войти."}, 401

    user_id = session['user_id']
    db = get_db()

    try:
        # Проверяем, существует ли новость
        news = db.execute('SELECT id FROM news WHERE id = ?', (news_id,)).fetchone()
        if not news:
            return {"error": "Новость не найдена."}, 404

        # Проверяем, поставил ли пользователь лайк ранее
        existing_like = db.execute('SELECT id FROM likes WHERE news_id = ? AND user_id = ?', (news_id, user_id)).fetchone()

        if existing_like:
            # Удаляем лайк, если он уже существует
            db.execute('DELETE FROM likes WHERE id = ?', (existing_like['id'],))
            action = 'unliked'
        else:
            # Добавляем лайк, если его нет
            db.execute('INSERT INTO likes (news_id, user_id) VALUES (?, ?)', (news_id, user_id))
            action = 'liked'

        db.commit()

        # Подсчитываем общее количество лайков для новости
        like_count = db.execute('SELECT COUNT(*) AS count FROM likes WHERE news_id = ?', (news_id,)).fetchone()['count']

        return {"action": action, "like_count": like_count}
    except Exception as e:
        logging.error(f"Ошибка при обработке лайка: {e}")
        return {"error": "Произошла ошибка при обработке лайка."}, 500
# Запуск приложения
if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_news_from_sources, 'interval', minutes=1, max_instances=1)
    scheduler.start()
    app.run(debug=True)