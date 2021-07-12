# Setup
```
pip install -r requirements.txt
```
# Running
1. Run the server
```
python -m chess_server
```
2. Run the client twice
```
python -m chess_client
```

# Custom bot
The client uses a RandomBot implementation by default, which will make valid, but random moves.
To swap this out, create an implementation of bot.py#Bot, and call from the chess client.