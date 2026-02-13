import asyncio
import logging
from os import getenv

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

# Settings
BOT_TOKEN = getenv("BOT_TOKEN")
MANAGER_GROUP_ID_RAW = getenv("MANAGER_GROUP_ID")

if not BOT_TOKEN or not MANAGER_GROUP_ID_RAW:
    raise ValueError("BOT_TOKEN и MANAGER_GROUP_ID обязательны!")

MANAGER_GROUP_ID = int(MANAGER_GROUP_ID_RAW)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# In-memory mappings:
# client_id -> current active thread_id
client_to_thread: dict[int, int] = {}
# thread_id -> client_id (to reply from any old thread too)
thread_to_client: dict[int, int] = {}

BOT_ID: int | None = None


class ClientStates(StatesGroup):
    waiting_for_estimate = State()
    waiting_for_price = State()
    waiting_for_decision = State()
    waiting_for_contact = State()
    waiting_for_feedback = State()


async def create_new_thread_for_client(message: Message) -> int:
    """
    Create new forum topic for the client and update mappings.
    """
    client_id = message.from_user.id
    username = message.from_user.username or "аноним"

    topic = await bot.create_forum_topic(
        chat_id=MANAGER_GROUP_ID,
        name=f"Клиент {client_id} ({username})"
    )

    thread_id = topic.message_thread_id

    # Update mappings
    client_to_thread[client_id] = thread_id
    thread_to_client[thread_id] = client_id

    # Notify managers inside that topic
    await bot.send_message(
        chat_id=MANAGER_GROUP_ID,
        message_thread_id=thread_id,
        text=f"Новый чат с клиентом: {message.from_user.full_name} (ID: {client_id})"
    )

    return thread_id


async def ensure_thread_for_client(message: Message) -> int:
    """
    Ensure active thread exists. If user didn't press /start, create one anyway.
    """
    client_id = message.from_user.id
    thread_id = client_to_thread.get(client_id)
    if thread_id:
        return thread_id
    return await create_new_thread_for_client(message)


def is_price_text(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    markers = ["руб", "₽", "$", "€", "цена", "стоимость", "итого", "total"]
    return any(m in t for m in markers)


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    # IMPORTANT: create a NEW thread every time /start is pressed
    await state.clear()

    await create_new_thread_for_client(message)

    await message.answer(
        "Чат создан. Отправьте смету (ссылка из Петровича / фото / файл). "
        "Дальше можно общаться свободно — я буду передавать сообщения менеджерам."
    )
    await state.set_state(ClientStates.waiting_for_estimate)


@dp.message(lambda m: m.chat.type == "private")
async def client_message_handler(message: Message, state: FSMContext):
    client_id = message.from_user.id
    current_state = await state.get_state()

    # Always forward/copy ANY client message to managers topic (free chat)
    thread_id = await ensure_thread_for_client(message)
    await bot.copy_message(
        chat_id=MANAGER_GROUP_ID,
        message_thread_id=thread_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )

    # Scenario logic WITHOUT blocking free chat
    text = (message.text or message.caption or "").strip().lower()

    if current_state == ClientStates.waiting_for_estimate:
        # If user likely sent an estimate, move forward; otherwise do nothing (chat is still free)
        if "petrovich.ru/cabinet/estimate/" in text or "estimate" in text or "смет" in text or message.photo or message.document:
            await message.answer("Принято. Передал менеджеру. Ожидайте цену.")
            await bot.send_message(
                chat_id=MANAGER_GROUP_ID,
                message_thread_id=thread_id,
                text="Клиент отправил смету. Рассчитайте и отправьте цену в этот топик."
            )
            await state.set_state(ClientStates.waiting_for_price)
        else:
            # Not blocking: just a light hint
            await message.answer("Если это смета — пришлите ссылку/фото/файл. Можно писать и дальше, я всё передам.")

    elif current_state == ClientStates.waiting_for_decision:
        if "да" == text or text.startswith("да "):
            await state.set_state(ClientStates.waiting_for_contact)
            await message.answer("Хорошо. Отправьте имя и номер телефона.", reply_markup=ReplyKeyboardRemove())
            await bot.send_message(
                chat_id=MANAGER_GROUP_ID,
                message_thread_id=thread_id,
                text="Клиент подтвердил: ДА. Запросите контакты/уточнения."
            )
        elif "нет" == text or text.startswith("нет "):
            await state.set_state(ClientStates.waiting_for_feedback)
            await message.answer("Понял. Напишите, что именно не устроило по позициям.", reply_markup=ReplyKeyboardRemove())
            await bot.send_message(
                chat_id=MANAGER_GROUP_ID,
                message_thread_id=thread_id,
                text="Клиент ответил: НЕТ. Запросите причину/обратную связь."
            )
        else:
            # Not blocking: message already copied to managers
            await message.answer("Для решения нажмите «Да» или «Нет» (или напишите словами).")

    elif current_state == ClientStates.waiting_for_contact:
        # Message already copied to managers; we just confirm and finish scenario
        await message.answer("Контакты передал менеджеру. С вами свяжутся.")
        await state.clear()

    elif current_state == ClientStates.waiting_for_feedback:
        await message.answer("Спасибо, передал менеджеру.")
        await state.clear()

    else:
        # Any other state: do nothing, chat is free
        pass


@dp.message(lambda m: m.chat.id == MANAGER_GROUP_ID and bool(getattr(m, "message_thread_id", None)))
async def manager_message_handler(message: Message):
    thread_id = message.message_thread_id
    client_id = thread_to_client.get(thread_id)
    if not client_id:
        return

    # Always copy ANY manager message to client (free chat)
    await bot.copy_message(
        chat_id=client_id,
        from_chat_id=MANAGER_GROUP_ID,
        message_id=message.message_id,
    )

    # If manager sent a price-like message, ask for decision (optional scenario)
    if message.text and is_price_text(message.text):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await bot.send_message(
            chat_id=client_id,
            text="Устраивает? (Да/Нет)",
            reply_markup=ke_
