#!/usr/bin/env python3
import discord
from discord.ext import commands
import json
import sys

# バッファリング無効化
sys.stdout = sys.stdout.__class__(sys.stdout.fileno(), 'w', buffering=1)

with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'✅ Bot ready! Logged in as {bot.user}', flush=True)
    print(f'Guild ID: {config["guild_id"]}', flush=True)

print("🚀 Starting bot...", flush=True)
bot.run(config['discord_token'])
