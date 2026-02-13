import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from os import getenv

# Настройки
BOT_TOKEN = getenv("BOT_TOKEN")
MANAGER_GROUP_ID = int(getenv("MANAGER_GROUP_ID"))

if not BOT_TOKEN or not MANAGER_GROUP_ID:
    raise ValueError("BOT_TOKEN и MANAGER_GROUP_ID обязательны!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

client_to_thread = {}

class ClientStates(StatesGroup):
    waiting_for_estimate = State()
    waiting_for_price = State()     # Новое: ожидание цены
    waiting_for_decision = State()
    waiting_for_contact = State()
    waiting_for_feedback = State()

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    logging.info(f"Start from user {message.from_user.id}")
    await message.answer(
        'Привет! Отправьте ссылку на смету из Petrovich '
        '<a href="https://petrovich.ru/cabinet/estimate/..."> (пример)</a> '
        'или прикрепите фото/файл.',
        parse_mode='HTML'
    )
    await state.set_state(ClientStates.waiting_for_estimate)

@dp.message(lambda message: message.chat.type == 'private')
async def client_message_handler(message: Message, state: FSMContext):
    client_id = message.from_user.id
    current_state = await state.get_state()
    content_type = message.content_type
    has_photo = bool(message.photo)
    has_document = bool(message.document)
    text = message.text or message.caption or ""
    logging.info(f"Received from {client_id}: state={current_state}, type={content_type}, text='{text}', photo={has_photo}, doc={has_document}")

    if client_id not in client_to_thread:
        logging.info(f"Creating thread for {client_id}")
        thread = await bot.create_forum_topic(
            chat_id=MANAGER_GROUP_ID,
            name=f"Клиент {client_id} ({message.from_user.username or 'аноним'})"
        )
        client_to_thread[client_id] = thread.message_thread_id
        await bot.send_message(MANAGER_GROUP_ID, thread.message_thread_id, f"Новый клиент: {message.from_user.full_name} (ID: {client_id})")

    thread_id = client_to_thread[client_id]

    if current_state == ClientStates.waiting_for_estimate:
        processed = False
        # Проверка текста (ссылка) или caption
        if text and ('petrovich.ru/cabinet/estimate/' in text or 'estimate' in text.lower() or 'smeta' in text.lower()):
            logging.info(f"Processing link: {text}")
            await bot.send_message(MANAGER_GROUP_ID, thread_id, f"Ссылка/текст от клиента: {text}")
            processed = True
        # Фото (с caption или без)
        if has_photo:
            logging.info("Processing photo")
            caption = text or "Фото сметы от клиента"
            await bot.send_photo(MANAGER_GROUP_ID, thread_id, message.photo[-1].file_id, caption=caption)
            processed = True
        # Файл (с caption или без)
        if has_document:
            logging.info("Processing document")
            caption = text or "Файл сметы от клиента"
            await bot.send_document(MANAGER_GROUP_ID, thread_id, message.document.file_id, caption=caption)
            processed = True

        if processed:
            await message.answer("Данные переданы. Ожидайте цену.")
            await bot.send_message(MANAGER_GROUP_ID, thread_id, "Рассчитайте и отправьте цену здесь.")
            await state.set_state(ClientStates.waiting_for_price)
        else:
            logging.info("Invalid input")
            await message.answer("Пожалуйста, отправьте ссылку на смету или фото/файл.")

    elif current_state == ClientStates.waiting_for_price:
        await message.answer("Ожидайте цены от менеджера. Если нужно добавить, напишите — перешлю.")

    elif current_state == ClientStates.waiting_for_decision:
        decision = (message.text or "").lower()
        if 'да' in decision:
            await state.set_state(ClientStates.waiting_for_contact)
            await message.answer("Отлично! Отправьте имя и номер телефона.")
            await bot.send_message(MANAGER_GROUP_ID, thread_id, "Клиент согласен.")
        elif 'нет' in decision:
            await state.set_state(ClientStates.waiting_for_feedback)
            await message.answer("Жаль. Какие позиции не устроили?")
            await bot.send_message(MANAGER_GROUP_ID, thread_id, "Клиент не согласен.")
        else:
            await message.answer("Ответьте 'да' или 'нет'.")

    elif current_state in (ClientStates.waiting_for_contact, ClientStates.waiting_for_feedback):
        await bot.send_message(MANAGER_GROUP_ID, thread_id, f"От клиента: {message.text}")
        if current_state == ClientStates.waiting_for_contact:
            await message.answer("Контакты переданы. Менеджер свяжется.")
        else:
            await message.answer("Спасибо за отзыв!")
        await state.clear()

    else:
        await bot.forward_message(MANAGER_GROUP_ID, thread_id, message.chat.id, message.message_id)

@dp.message(lambda message: message.chat.id == MANAGER_GROUP_ID and message.message_thread_id)
async def manager_message_handler(message: Message, state: FSMContext):
    thread_id = message.message_thread_id
    client_id = next((cid for cid, tid in client_to_thread.items() if tid == thread_id), None)
    if not client_id:
        return

    logging.info(f"Manager msg in {thread_id}: {message.text}")
    await bot.send_message(client_id, message.text)

    if 'цена' in message.text.lower() or any(c in message.text for c in ['руб', '₽', '$']):
        keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]], resize_keyboard=True, one_time_keyboard=True)
        await bot.send_message(client_id, "Устраивает? (да/нет)", reply_markup=keyboard)
        client_state = FSMContext(storage=dp.storage, key=types.StorageKey(bot_id=bot.id, chat_id=client_id, user_id=client_id))
        await client_state.set_state(ClientStates.waiting_for_decision)

async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot, allowed_updates=["message", "photo", "document"])

if __name__ == '__main__':
    asyncio.run(main())
