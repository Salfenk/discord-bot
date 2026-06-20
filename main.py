import asyncio
import logging
import os
import json
import tempfile
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

import discord
from discord.ext import commands
from discord import FFmpegPCMAudio
import aiohttp
import aiohttp.client_exceptions
from aiohttp import web
import vk_api
from vk_api.audio import VkAudio
import yt_dlp

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Переменные окружения
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
VK_TOKEN = os.getenv('VK_TOKEN')
VK_USER_ID = os.getenv('VK_USER_ID')
PORT = int(os.getenv('PORT', 8080))

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN не установлен в переменных окружения")
if not VK_TOKEN:
    raise ValueError("VK_TOKEN не установлен в переменных окружения")

# Настройки бота
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    help_command=commands.DefaultHelpCommand()
)

class Track:
    """Класс для хранения информации о треке"""
    def __init__(self, title: str, url: str, duration: int, artist: str = "Неизвестен"):
        self.title = title
        self.url = url
        self.duration = duration
        self.artist = artist
    
    def __str__(self):
        return f"{self.artist} - {self.title} ({self.duration//60}:{self.duration%60:02d})"

class MusicPlayer:
    """Управление воспроизведением музыки"""
    def __init__(self, ctx):
        self.ctx = ctx
        self.queue = []
        self.current_track = None
        self.is_playing = False
        self.is_paused = False
        self.voice_client = None
        self.current_embed = None
        self.temp_dir = tempfile.mkdtemp()
        logger.info(f"Создана временная директория: {self.temp_dir}")
    
    async def connect(self):
        """Подключение к голосовому каналу"""
        if not self.ctx.author.voice:
            await self.ctx.send("❌ Вы не в голосовом канале!")
            return False
        
        channel = self.ctx.author.voice.channel
        
        if self.ctx.voice_client:
            self.voice_client = self.ctx.voice_client
            await self.voice_client.move_to(channel)
        else:
            self.voice_client = await channel.connect()
        
        return True
    
    async def play_next(self):
        """Воспроизведение следующего трека в очереди"""
        if not self.queue:
            self.is_playing = False
            self.current_track = None
            await self.update_status("⏸ Очередь пуста")
            return
        
        if not self.voice_client or not self.voice_client.is_connected():
            self.is_playing = False
            await self.ctx.send("❌ Бот отключен от голосового канала")
            return
        
        track = self.queue.pop(0)
        self.current_track = track
        self.is_playing = True
        self.is_paused = False
        
        # Скачиваем трек во временную папку
        audio_file = await self.download_track(track)
        if not audio_file:
            await self.ctx.send(f"❌ Не удалось загрузить трек: {track.title}")
            await self.play_next()
            return
        
        try:
            # Воспроизводим с FFmpeg с опциями для стабильности
            source = FFmpegPCMAudio(
                audio_file,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn -bufsize 64k"
            )
            
            def after_playing(error):
                if error:
                    logger.error(f"Ошибка воспроизведения: {error}")
                asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop)
            
            self.voice_client.play(source, after=after_playing)
            await self.update_status(f"🎵 Сейчас играет: {track}")
            
            # Очищаем временный файл через 5 минут после воспроизведения
            async def cleanup():
                await asyncio.sleep(300)
                try:
                    if os.path.exists(audio_file):
                        os.remove(audio_file)
                        logger.info(f"Удален временный файл: {audio_file}")
                except Exception as e:
                    logger.error(f"Ошибка удаления файла: {e}")
            
            asyncio.create_task(cleanup())
            
        except Exception as e:
            logger.error(f"Ошибка воспроизведения: {e}")
            await self.ctx.send(f"❌ Ошибка воспроизведения: {str(e)}")
            await self.play_next()
    
    async def download_track(self, track: Track) -> Optional[str]:
        """Скачивание трека из ВК во временную папку"""
        try:
            # Используем yt-dlp для скачивания из ВК
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': f'{self.temp_dir}/%(title)s.%(ext)s',
                'quiet': True,
                'no_warnings': True,
                'extractaudio': True,
                'audioformat': 'mp3',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(track.url, download=True)
                filename = ydl.prepare_filename(info)
                # Меняем расширение на mp3 если было другое
                if filename.endswith('.webm') or filename.endswith('.m4a'):
                    filename = filename.rsplit('.', 1)[0] + '.mp3'
                return filename
                
        except Exception as e:
            logger.error(f"Ошибка скачивания трека {track.title}: {e}")
            return None
    
    async def add_track(self, track: Track):
        """Добавление трека в очередь"""
        self.queue.append(track)
        await self.update_status(f"➕ Добавлен в очередь: {track}")
        
        if not self.is_playing:
            await self.play_next()
    
    async def add_tracks(self, tracks: List[Track]):
        """Добавление нескольких треков в очередь"""
        for track in tracks:
            self.queue.append(track)
        
        await self.update_status(f"➕ Добавлено {len(tracks)} треков в очередь")
        
        if not self.is_playing:
            await self.play_next()
    
    async def skip(self):
        """Пропуск текущего трека"""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            await self.ctx.send("⏭ Пропущен текущий трек")
        else:
            await self.ctx.send("❌ Сейчас ничего не играет")
    
    async def pause(self):
        """Пауза"""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            self.is_paused = True
            await self.update_status("⏸ Пауза")
            await self.ctx.send("⏸ Воспроизведение приостановлено")
        else:
            await self.ctx.send("❌ Сейчас ничего не играет")
    
    async def resume(self):
        """Возобновление"""
        if self.voice_client and self.is_paused:
            self.voice_client.resume()
            self.is_paused = False
            await self.update_status(f"▶️ Возобновлено: {self.current_track}")
            await self.ctx.send("▶️ Воспроизведение возобновлено")
        else:
            await self.ctx.send("❌ Нет приостановленного воспроизведения")
    
    async def clear(self):
        """Очистка очереди"""
        self.queue.clear()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.is_playing = False
        self.current_track = None
        await self.update_status("🗑 Очередь очищена")
        await self.ctx.send("🗑 Очередь очищена")
    
    async def shuffle(self):
        """Перемешивание очереди"""
        import random
        random.shuffle(self.queue)
        await self.ctx.send("🔀 Очередь перемешана")
    
    async def show_queue(self):
        """Показ очереди"""
        if not self.queue and not self.current_track:
            await self.ctx.send("📭 Очередь пуста")
            return
        
        embed = discord.Embed(title="📋 Очередь воспроизведения", color=discord.Color.blue())
        
        if self.current_track:
            embed.add_field(
                name="🎵 Сейчас играет",
                value=str(self.current_track),
                inline=False
            )
        
        if self.queue:
            queue_list = []
            for i, track in enumerate(self.queue[:10], 1):
                queue_list.append(f"{i}. {track}")
            embed.add_field(
                name=f"📝 В очереди ({len(self.queue)} треков)",
                value="\n".join(queue_list),
                inline=False
            )
        
        await self.ctx.send(embed=embed)
    
    async def update_status(self, status: str):
        """Обновление статуса бота"""
        await bot.change_presence(
            activity=discord.Game(name=status[:100])
        )
    
    async def leave(self):
        """Выход из голосового канала"""
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None
            self.is_playing = False
            self.current_track = None
            self.queue.clear()
            
            # Удаляем временную директорию
            try:
                shutil.rmtree(self.temp_dir)
                logger.info(f"Удалена временная директория: {self.temp_dir}")
            except Exception as e:
                logger.error(f"Ошибка удаления временной директории: {e}")
            
            await self.ctx.send("👋 Бот покинул голосовой канал")
            await self.update_status("🚪 Ожидание команд...")
    
    def __del__(self):
        """Очистка временных файлов при удалении объекта"""
        try:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except:
            pass

class VKMusicSearch:
    """Поиск музыки в ВКонтакте"""
    def __init__(self):
        self.vk_session = vk_api.VkApi(token=VK_TOKEN)
        self.vk = self.vk_session.get_api()
        self.vk_audio = VkAudio(self.vk_session)
    
    async def search_track(self, query: str) -> Optional[Track]:
        """Поиск одного трека"""
        try:
            # Используем официальный метод audio.search через vk_api
            audio_list = self.vk_audio.search(q=query, count=1)
            
            if not audio_list:
                return None
            
            audio = audio_list[0]
            track = Track(
                title=audio['title'],
                url=audio['url'],
                duration=audio['duration'],
                artist=audio['artist']
            )
            return track
            
        except Exception as e:
            logger.error(f"Ошибка поиска трека: {e}")
            return None
    
    async def search_playlist(self, query: str, count: int = 5) -> List[Track]:
        """Поиск нескольких треков (имитация плейлиста)"""
        tracks = []
        try:
            audio_list = self.vk_audio.search(q=query, count=count)
            
            for audio in audio_list:
                track = Track(
                    title=audio['title'],
                    url=audio['url'],
                    duration=audio['duration'],
                    artist=audio['artist']
                )
                tracks.append(track)
            
            return tracks
            
        except Exception as e:
            logger.error(f"Ошибка поиска плейлиста: {e}")
            return []

# Хранилище плееров
players = {}

async def get_player(ctx) -> MusicPlayer:
    """Получение или создание плеера для гильдии"""
    guild_id = ctx.guild.id
    if guild_id not in players:
        players[guild_id] = MusicPlayer(ctx)
    return players[guild_id]

# Команды бота
@bot.command(name='play', aliases=['p'])
async def play(ctx, *, query: str):
    """Воспроизведение трека из ВКонтакте"""
    player = await get_player(ctx)
    
    if not await player.connect():
        return
    
    # Поиск трека
    vk_search = VKMusicSearch()
    track = await vk_search.search_track(query)
    
    if not track:
        await ctx.send(f"❌ Трек '{query}' не найден в ВКонтакте")
        return
    
    await player.add_track(track)

@bot.command(name='playlist', aliases=['pl'])
async def playlist(ctx, *, query: str):
    """Добавление плейлиста (5 треков) из ВКонтакте"""
    player = await get_player(ctx)
    
    if not await player.connect():
        return
    
    # Поиск плейлиста
    vk_search = VKMusicSearch()
    tracks = await vk_search.search_playlist(query, count=5)
    
    if not tracks:
        await ctx.send(f"❌ Треки по запросу '{query}' не найдены")
        return
    
    await player.add_tracks(tracks)
    await ctx.send(f"✅ Добавлено {len(tracks)} треков в очередь")

@bot.command(name='skip', aliases=['s'])
async def skip(ctx):
    """Пропуск текущего трека"""
    player = players.get(ctx.guild.id)
    if player:
        await player.skip()
    else:
        await ctx.send("❌ Нет активного плеера")

@bot.command(name='queue', aliases=['q'])
async def queue(ctx):
    """Показать очередь"""
    player = players.get(ctx.guild.id)
    if player:
        await player.show_queue()
    else:
        await ctx.send("❌ Нет активного плеера")

@bot.command(name='pause')
async def pause(ctx):
    """Пауза"""
    player = players.get(ctx.guild.id)
    if player:
        await player.pause()
    else:
        await ctx.send("❌ Нет активного плеера")

@bot.command(name='resume', aliases=['r'])
async def resume(ctx):
    """Возобновление воспроизведения"""
    player = players.get(ctx.guild.id)
    if player:
        await player.resume()
    else:
        await ctx.send("❌ Нет активного плеера")

@bot.command(name='clear')
async def clear(ctx):
    """Очистка очереди"""
    player = players.get(ctx.guild.id)
    if player:
        await player.clear()
    else:
        await ctx.send("❌ Нет активного плеера")

@bot.command(name='shuffle')
async def shuffle(ctx):
    """Перемешивание очереди"""
    player = players.get(ctx.guild.id)
    if player:
        await player.shuffle()
    else:
        await ctx.send("❌ Нет активного плеера")

@bot.command(name='leave', aliases=['stop'])
async def leave(ctx):
    """Выход из голосового канала"""
    player = players.get(ctx.guild.id)
    if player:
        await player.leave()
        del players[ctx.guild.id]
    else:
        await ctx.send("❌ Нет активного плеера")

@bot.command(name='now', aliases=['np'])
async def now_playing(ctx):
    """Показать текущий трек"""
    player = players.get(ctx.guild.id)
    if player and player.current_track:
        embed = discord.Embed(title="🎵 Сейчас играет", color=discord.Color.green())
        embed.add_field(name="Трек", value=str(player.current_track), inline=False)
        embed.add_field(name="В очереди", value=len(player.queue), inline=True)
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ Сейчас ничего не играет")

# HTTP сервер для Keep-Alive
async def health_check(request):
    """Проверка состояния бота"""
    return web.Response(text="OK")

async def http_server():
    """Запуск HTTP сервера"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
    await site.start()
    logger.info(f"HTTP сервер запущен на порту {PORT}")
    
    # Держим сервер запущенным
    await asyncio.Event().wait()

@bot.event
async def on_ready():
    """Событие готовности бота"""
    logger.info(f'Бот {bot.user} готов к работе!')
    logger.info(f'Подключен к {len(bot.guilds)} серверам')
    
    await bot.change_presence(
        activity=discord.Game(name="/play для музыки 🎵")
    )

@bot.event
async def on_voice_state_update(member, before, after):
    """Обработка изменений голосовых каналов"""
    if member == bot.user:
        # Если бота выгнали из канала
        if before.channel and not after.channel:
            guild_id = before.channel.guild.id
            if guild_id in players:
                player = players[guild_id]
                player.is_playing = False
                player.current_track = None
                player.queue.clear()
                await player.update_status("🚪 Бот отключен")
                # Удаляем временные файлы
                try:
                    shutil.rmtree(player.temp_dir)
                except:
                    pass
                del players[guild_id]
                logger.info(f"Бот отключен от канала {before.channel.name}")

@bot.event
async def on_command_error(ctx, error):
    """Обработка ошибок команд"""
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Не хватает аргументов: {error}")
    else:
        logger.error(f"Ошибка в команде {ctx.command}: {error}")
        await ctx.send(f"❌ Произошла ошибка: {str(error)[:100]}")

async def main():
    """Главная функция"""
    # Запускаем HTTP сервер в фоновом режиме
    asyncio.create_task(http_server())
    
    # Запускаем бота
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())