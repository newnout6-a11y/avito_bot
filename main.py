    import logging
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from background import keep_alive

API_TOKEN = '8203118488:AAGC1EQavBt0u9suYQ32qt4owkU_VuubWG0'  # никому не показывай настоящий токен!

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(F.text.in_(['/start', '/help']))
async def send_welcome(message: types.Message):
    await message.answer("Hi!\nI'm EchoBot!\nPowered by aiogram 3.x")

@dp.message(F.text.regexp(r'(^cat[s]?$|puss)'))
async def send_cat(message: types.Message):
    photo = FSInputFile('data/cats.jpg')
    await message.answer_photo(photo, caption='Cats are here 😺')

@dp.message(F.text)
async def echo(message: types.Message):
    if message.text:
        await message.answer(message.text)

async def main():
    keep_alive()  # если у тебя такой модуль есть
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
