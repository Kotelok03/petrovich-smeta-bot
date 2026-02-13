import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from os import getenv

# Настройки из переменных окружения (для Railway)
BOT_TOKEN = getenv("BOT_TOKEN")
MANAGER_GROUP_ID = int(getenv("MANAGER_GROUP_ID"))  # Например, -1001234567890

if not BOT_TOKEN or not MANAGER_GROUP_ID:
    raise ValueError("BOT_TOKEN и MANAGER_GROUP_ID обязательны!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# Словарь для ассоциаций клиент → тема (в production замени на базу данных)
client_to_thread = {}

# Состояния FSM для диалога
class ClientStates(StatesGroup):
    waiting_for_estimate = State()  # Ждём смету
    waiting_for_decision = State()  # Ждём да/нет
    waiting_for_contact = State()  # Ждём контакты
    waiting_for_feedback = State()  # Ждём отзыв

# Команда /start
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await message.answer(
        'Привет! Отправьте ссылку на смету из Petrovich '
        '<a href="https://petrovich.ru/cabinet/estimate/..."> (пример)</a> '
        'или прикрепите фото/файл.',
        parse_mode='HTML'
    )
    await state.set_state(ClientStates.waiting_for_estimate)

# Обработка сообщений от клиентов (в личке)
@dp.message(lambda message: message.chat.type == 'private')
async def client_message_handler(message: Message, state: FSMContext):
    client_id = message.from_user.id
    current_state = await state.get_state()

    # Создаём тему, если нет
    if client_id not in client_to_thread:
        thread = await bot.create_forum_topic(
            chat_id=MANAGER_GROUP_ID,
            name=f"Клиент {client_id} ({message.from_user.username or 'аноним'})"
        )
        client_to_thread[client_id] = thread.message_thread_id
        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            message_thread_id=thread.message_thread_id,
            text=f"Новый клиент: {message.from_user.full_name} (ID: {client_id})"
        )

    thread_id = client_to_thread[client_id]

    # Шаг 1: Ждём смету
    if current_state == ClientStates.waiting_for_estimate:
        if message.text and 'petrovich.ru/cabinet/estimate/' in message.text:
            await bot.send_message(MANAGER_GROUP_ID, thread_id, f"Ссылка от клиента: {message.text}")
        elif message.photo:
            await bot.send_photo(MANAGER_GROUP_ID, thread_id, message.photo[-1].file_id, caption="Фото сметы от клиента")
        elif message.document:
            await bot.send_document(MANAGER_GROUP_ID, thread_id, message.document.file_id, caption="Файл сметы от клиента")
        else:
            await message.answer("Пожалуйста, отправьте ссылку или фото/файл.")
            return
        await message.answer("Данные переданы. Ожидайте цену от менеджера.")
        await bot.send_message(MANAGER_GROUP_ID, thread_id, "Рассчитайте и отправьте цену (ответьте в этой теме).")
        # Состояние не меняем — цена придёт от менеджера

    # Шаг 3: Да/нет после цены
    elif current_state == ClientStates.waiting_for_decision:
        decision = message.text.lower()
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

    # Шаг 4: Контакты или отзыв
    elif current_state in (ClientStates.waiting_for_contact, ClientStates.waiting_for_feedback):
        await bot.send_message(MANAGER_GROUP_ID, thread_id, f"От клиента: {message.text}")
        if current_state == ClientStates.waiting_for_contact:
            await message.answer("Контакты переданы. Менеджер свяжется.")
        else:
            await message.answer("Спасибо за отзыв!")
        await state.clear()

    else:
        # Пересылка любого другого сообщения
        await bot.forward_message(MANAGER_GROUP_ID, thread_id, message.chat.id, message.message_id)

# Обработка от менеджеров (в группе, в теме)
@dp.message(lambda message: message.chat.id == MANAGER_GROUP_ID and message.message_thread_id)
async def manager_message_handler(message: Message, state: FSMContext):
    thread_id = message.message_thread_id
    client_id = next((cid for cid, tid in client_to_thread.items() if tid == thread_id), None)
    if not client_id:
        return

    # Пересылка менеджеру → клиенту
    await bot.send_message(client_id, message.text)

    # Если это цена — просим да/нет и меняем состояние
    if 'цена' in message.text.lower() or any(c in message.text for c in ['руб', '₽', '$']):
        await bot.send_message(client_id, "Устраивает? (да/нет)")
        client_state = FSMContext(storage=dp.storage, key=types.StorageKey(bot_id=bot.id, chat_id=client_id, user_id=client_id))
        await client_state.set_state(ClientStates.waiting_for_decision)

async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
