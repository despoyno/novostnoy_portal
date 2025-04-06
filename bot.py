import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from datetime import datetime
import logging
from TOKEN import TOKEN

# Конфигурация бота
API_TOKEN = TOKEN
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Подключение к базе данных
DATABASE = 'users.db'

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# Инициализация базы данных
def init_db():
    with get_db() as db:
        # Таблица пользователей
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                unique_code TEXT UNIQUE,
                telegram_chat_id INTEGER
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
        # Таблица отправленных новостей
        db.execute('''
            CREATE TABLE IF NOT EXISTS sent_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER UNIQUE NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        db.commit()

# Состояния FSM для создания постов
class CreatePost(StatesGroup):
    title = State()
    content = State()
    category = State()

# Клавиатуры
def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Админ-панель")]],
        resize_keyboard=True
    )

def get_admin_panel():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Создать пост")],
            [KeyboardButton(text="Модерация новостей")]
        ],
        resize_keyboard=True
    )

# Обработчик команды /start
@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    await state.clear()
    chat_id = message.chat.id
    args = message.text.split()
    unique_code = args[1] if len(args) > 1 else None

    if unique_code:
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE unique_code = ?', (unique_code,)).fetchone()
        
        if user:
            try:
                db.execute('UPDATE users SET telegram_chat_id = ? WHERE unique_code = ?', 
                          (chat_id, unique_code))
                db.commit()
                if user['role'] == 'admin':
                    await message.answer("Админ привязан!", reply_markup=get_admin_keyboard())
                else:
                    await message.answer("Вы подписаны на уведомления!")
            except sqlite3.IntegrityError:
                await message.answer("Этот Telegram уже привязан!")
        else:
            await message.answer("Неверный код!")
        db.close()
    else:
        await message.answer("Используйте: /start <ваш_код>")

# Админ-панель
@dp.message(F.text == "Админ-панель")
async def admin_panel(message: Message):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE telegram_chat_id = ?', 
                     (message.chat.id,)).fetchone()
    db.close()
    
    if user and user['role'] == 'admin':
        await message.answer("Админ-панель:", reply_markup=get_admin_panel())
    else:
        await message.answer("Нет доступа!")

# Создание поста - начало
@dp.message(F.text == "Создать пост")
async def create_post(message: Message, state: FSMContext):
    await state.set_state(CreatePost.title)
    await message.answer("Введите заголовок:")

# Обработка заголовка
@dp.message(CreatePost.title)
async def process_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(CreatePost.content)
    await message.answer("Введите содержание:")

# Обработка содержания
@dp.message(CreatePost.content)
async def process_content(message: Message, state: FSMContext):
    await state.update_data(content=message.text)
    await state.set_state(CreatePost.category)
    await message.answer("Выберите категорию (1-5):\n"
                        "1 - Медицина\n"
                        "2 - Политика\n"
                        "3 - Учёба\n"
                        "4 - Спорт\n"
                        "5 - Другое")

# Обработка категории
@dp.message(CreatePost.category)
async def process_category(message: Message, state: FSMContext):
    if message.text.isdigit() and 1 <= int(message.text) <= 5:
        data = await state.get_data()
        category = int(message.text)
        
        db = get_db()
        user = db.execute('SELECT id FROM users WHERE telegram_chat_id = ?', 
                         (message.chat.id,)).fetchone()
        
        if user:
            db.execute('''
                INSERT INTO news (title, content, category, user_id, moderated)
                VALUES (?, ?, ?, ?, 0)
            ''', (data['title'], data['content'], category, user['id']))
            db.commit()
            await message.answer("Пост отправлен на модерацию!")
        else:
            await message.answer("Ошибка: пользователь не найден!")
        
        db.close()
        await state.clear()
    else:
        await message.answer("Неверная категория! Введите число от 1 до 5")

# Модерация новостей
@dp.message(F.text == "Модерация новостей")
async def moderate_news(message: Message):
    db = get_db()
    news = db.execute('''
        SELECT n.*, u.username 
        FROM news n
        LEFT JOIN users u ON n.user_id = u.id
        WHERE n.moderated = 0
    ''').fetchall()
    db.close()

    if news:
        for item in news:
            category_names = {
                1: "медицинскую",
                2: "политическую",
                3: "учебную",
                4: "спортивную",
                5: "другую"
            }
            category_text = category_names.get(item['category'], "другую")
            
            approve_btn = InlineKeyboardButton(
                text="✅ Одобрить",
                callback_data=f"approve_{item['id']}"
            )
            reject_btn = InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"reject_{item['id']}"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[approve_btn, reject_btn]])
            
            text = (
                f"ID: {item['id']}\n"
                f"Автор: {item['username'] or 'Неизвестно'}\n"
                f"Заголовок: {item['title']}\n"
                f"Категория: {category_text}\n"
                f"Текст: {item['content']}"
            )
            await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer("Нет новостей на модерации")

# Обработка одобрения новости
@dp.callback_query(F.data.startswith("approve_"))
async def approve_news(callback: types.CallbackQuery):
    news_id = int(callback.data.split("_")[1])
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    db = get_db()
    try:
        db.execute('''
            UPDATE news 
            SET moderated = 1, published_at = ? 
            WHERE id = ?
        ''', (current_time, news_id))
        db.commit()
        await callback.answer("Новость одобрена!")
        await callback.message.edit_text(f"✅ Одобрено: {callback.message.text}")
    except Exception as e:
        await callback.answer("Ошибка при одобрении")
        logging.error(f"Ошибка одобрения новости: {e}")
    finally:
        db.close()

# Обработка отклонения новости
@dp.callback_query(F.data.startswith("reject_"))
async def reject_news(callback: types.CallbackQuery):
    news_id = int(callback.data.split("_")[1])
    
    db = get_db()
    try:
        db.execute('DELETE FROM news WHERE id = ?', (news_id,))
        db.commit()
        await callback.answer("Новость отклонена!")
        await callback.message.edit_text(f"❌ Отклонено: {callback.message.text}")
    except Exception as e:
        await callback.answer("Ошибка при отклонении")
        logging.error(f"Ошибка отклонения новости: {e}")
    finally:
        db.close()

# Отправка уведомлений
async def send_notification(title, content, category):
    category_names = {
        1: "медицинскую",
        2: "политическую",
        3: "учебную",
        4: "спортивную",
        5: "другую"
    }
    category_text = category_names.get(category, "другую")
    
    message_text = (
        f"📰 Новая новость!\n\n"
        f"Заголовок: {title}\n"
        f"Содержание: {content}"
    )
    
    db = get_db()
    users = db.execute('SELECT telegram_chat_id FROM users WHERE telegram_chat_id IS NOT NULL').fetchall()
    db.close()
    
    for user in users:
        try:
            await bot.send_message(chat_id=user['telegram_chat_id'], text=message_text)
        except Exception as e:
            logging.error(f"Ошибка отправки {user['telegram_chat_id']}: {e}")

# Проверка новых новостей
async def check_notifications():
    while True:
        db = get_db()
        news = db.execute('''
            SELECT n.id, n.title, n.content, n.category 
            FROM news n
            LEFT JOIN sent_news sn ON n.id = sn.news_id
            WHERE n.moderated = 1 AND sn.news_id IS NULL
        ''').fetchall()
        
        if news:
            for item in news:
                await send_notification(
                    title=item['title'],
                    content=item['content'],
                    category=item['category']
                )
                db.execute('INSERT OR IGNORE INTO sent_news (news_id) VALUES (?)', (item['id'],))
                db.commit()
        db.close()
        await asyncio.sleep(60)

# Команда /stop для отмены подписки
@dp.message(Command("stop"))
async def stop_command(message: Message):
    db = get_db()
    user = db.execute('SELECT id FROM users WHERE telegram_chat_id = ?', 
                     (message.chat.id,)).fetchone()
    
    if user:
        db.execute('UPDATE users SET telegram_chat_id = NULL WHERE id = ?', 
                  (user['id'],))
        db.commit()
        await message.answer("Вы отписались от уведомлений.")
    else:
        await message.answer("Вы и так не подписаны.")
    
    db.close()

# Запуск бота
async def main():
    init_db()
    asyncio.create_task(check_notifications())
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())