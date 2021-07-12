import asyncio
import json
import uuid

import chess
import websockets

from chess_bot.bot import Bot, RandomBot


async def run_for_one():
    uri = "ws://localhost:8765"
    try:
        board: chess.Board = chess.Board()
        bot: Bot = RandomBot()
        username = f"test_client_{uuid.uuid4()}"
        ws: websockets.WebSocketClientProtocol = await websockets.connect(uri)
        out_msg = json.dumps({"user": username})
        await ws.send(out_msg)

        new_game_found_msg: dict = await receive_json(ws)

        if new_game_found_msg.get("is_white") is True:
            await send_next_move(username, board, bot, ws)

        while True:
            board_update_msg: dict = await receive_json(ws)
            board = chess.Board(fen=board_update_msg.get("fen_board"))
            is_game_running = board_update_msg.get("outcome") == "None"
            if not is_game_running:
                winner = board_update_msg.get("winner")
                print(f"Winner {winner}")
                break
            if board_update_msg.get("is_next_to_move") is True:
                await send_next_move(username, board, bot, ws)

    except Exception as e:
        print(e)


async def send_next_move(username, board, bot, ws):
    move = bot.next_move(board)
    msg = json.dumps({"user": username, "type": "move", "uci_move": move.uci()})
    print("Sending move message:", msg)
    await ws.send(msg)


async def receive_json(ws) -> dict:
    print("Waiting for message")
    msg = await ws.recv()
    print("Received message:", msg)
    return json.loads(msg)


if __name__ == '__main__':
    RandomBot()
    asyncio.get_event_loop().run_until_complete(run_for_one())
