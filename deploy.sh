#!/bin/bash

# Navigate to the TradingApp directory
cd ~/TradingApp

# Pull the latest code from the GitHub repository
git pull origin master

# Activate the virtual environment
source venv/bin/activate

# Check if the existing tmux session is running, and if so, kill it gracefully
if tmux has-session -t trading_bot 2>/dev/null; then
    # Send termination signal to the Python process running inside the tmux session
    tmux send-keys -t trading_bot C-c
    sleep 2
    tmux kill-session -t trading_bot
    echo "Killed the existing tmux session: trading_bot"
else
    echo "No existing tmux session named trading_bot found."
fi

# Start a new tmux session running the bot
tmux new-session -d -s trading_bot 'python trading_bot.py'
echo "Started a new tmux session: trading_bot"
