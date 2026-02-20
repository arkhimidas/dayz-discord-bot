#!/bin/bash
# Päivitä koodi GitHubista ja käynnistä botti uudelleen

cd ~/dayz-discord-bot || exit 1
echo "Vedetään uusimmat päivitykset..."
git pull || exit 1

# Aktivoi virtuaaliympäristö (muokkaa polku tarvittaessa)
source .venv/bin/activate

echo "Käynnistetään botti..."
# Tapa mahdollinen vanha bottiprosessi (muokkaa prosessin tunnistetta tarvittaessa)
pkill -f bot.py
nohup python3 bot.py &

echo "Botti päivitetty ja käynnistetty taustalle!"