# Aviator Crash Game

## Overview
A browser-based multiplayer Aviator crash game with server-controlled game flow. Players place bets and attempt to cash out before the multiplier "crashes." The game features real-time synchronization across all connected clients, provably fair crash point generation with a ~4% house edge, and a Telegram bot integration for user management.

## User Preferences
Preferred communication style: Simple, everyday language.

## System Architecture

### Frontend Architecture
- **Single-page application** using vanilla HTML, CSS, and JavaScript (no build tools or frameworks)
- **UI Language**: Amharic (Ethiopian)
- **Real-time updates** via Socket.IO client for WebSocket communication
- **Responsive design** with CSS custom properties for theming
- **Sidebar navigation** with slide-in menu pattern

### Backend Architecture
- **Flask** web framework serving static files and handling HTTP requests
- **Flask-SocketIO** with Eventlet async mode for real-time bidirectional WebSocket communication
- **Server-controlled game loop** managing all game phases:
  1. 7-second countdown phase
  2. Active game phase with exponentially increasing multiplier
  3. Crash event at predetermined point
  4. 3-second pause before next round

### Game Logic
- **Provably fair system**: Crash points generated using SHA-256 hashing of combined server/client seeds
- **House edge**: ~4% built into crash point algorithm
- **Instant crash**: 1% chance of crashing at 1.00x multiplier
- **Independence**: Player actions (bets, cash-outs) don't affect crash outcomes
- **In-memory user store**: Users dictionary for session management

### Telegram Bot Integration
- Bot integration using python-telegram-bot library
- Environment variables: `TELEGRAM_BOT_TOKEN` and `REPLIT_DEV_DOMAIN`
- Keyboard button interface for user interaction

## External Dependencies

### Python Packages
- **Flask** - Web framework for serving the application
- **Flask-SocketIO** - WebSocket support for real-time game state synchronization
- **Eventlet** - Async networking library required for Flask-SocketIO async mode
- **python-telegram-bot** - Telegram bot API wrapper (telegram, telegram.ext modules)

### Environment Variables
- `TELEGRAM_BOT_TOKEN` - Authentication token for Telegram bot
- `REPLIT_DEV_DOMAIN` - Domain URL for the Replit deployment

### Runtime Configuration
- **Python 3.x** - Server runtime
- **Port 5000** - Default server port
- **CORS** - Enabled for all origins in Socket.IO configuration