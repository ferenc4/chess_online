import asyncio
import copy
import json
import logging
import sys
from dataclasses import dataclass
from typing import Dict, Optional, Set, Coroutine

import chess
from websockets.exceptions import ConnectionClosed
from websockets.legacy.protocol import WebSocketCommonProtocol
from websockets.legacy.server import serve

from chess_server.api.server import GenericServer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@dataclass
class Session:
    websocket: WebSocketCommonProtocol
    username: str

    @property
    def id(self):
        return f"{self.username}@{get_cli_address(self.websocket)}"


@dataclass
class Game:
    opponents: Dict[str, str]
    first_user: str
    board: chess.Board

    def get_current_turn_user(self):
        return self.first_user if self.board.turn is chess.WHITE else self.opponents.get(self.first_user)


@dataclass
class ServerContext:
    games_by_user: Dict[str, Game]
    opponents: Dict[str, str]
    queued_up_user: Optional[str]
    users_by_connection: Dict[WebSocketCommonProtocol, str]
    connections_by_user: Dict[str, WebSocketCommonProtocol]
    user_mgmt_lock: asyncio.Lock
    viewer_set_by_viewed: Dict[str, Set[str]]


def get_cli_address(websocket: Optional[WebSocketCommonProtocol]) -> str:
    return None if websocket is None else f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"


async def send(user: str,
               server_context: ServerContext,
               message: str):
    websocket = server_context.connections_by_user.get(user, None)
    client_id = get_cli_address(websocket)
    try:
        logger.info(f"{client_id} Server sending message < {message} >")
        await websocket.send(message)
    except Exception:
        logger.exception(f"{client_id} Exception occurred while sending message < {message} >")
        try:
            websocket.fail_connection(1011, f"An error occurred, ending connection.")
        except Exception:
            logger.exception(f"{client_id} Websocket couldn't be failed.")
        await unregister(websocket=websocket, server_context=server_context)


def register(user: str,
             websocket: WebSocketCommonProtocol,
             server_context: ServerContext):
    existing_user: Optional[str] = server_context.users_by_connection.get(websocket, None)
    existing_connection: Optional[WebSocketCommonProtocol] = server_context.connections_by_user.get(user, None)
    # todo check existing values are consistent with new ones, if there are any

    if existing_user is not None:
        return
    server_context.users_by_connection[websocket] = user
    server_context.connections_by_user[user] = websocket

    if server_context.queued_up_user is None:
        server_context.queued_up_user = user
        return None

    game: Game = generate_game(user1=server_context.queued_up_user, user2=user)
    server_context.queued_up_user = None
    for game_user in game.opponents.keys():
        server_context.games_by_user[game_user] = game
        game_conn = server_context.connections_by_user.get(game_user)
        game_session_id = get_cli_address(game_conn)
        logger.info(f"{game_session_id} Registered new game for {game_user}")
    return game


async def start_game(server_context: ServerContext, game: Game):
    coroutines = []
    for user in game.opponents.keys():
        coroutines.append(send(user=user, server_context=server_context,
                               message=json.dumps({"type": "new_game_found", "is_white": user == game.first_user})))
    await asyncio.wait(coroutines)


async def send_viewer_update(game: Game,
                             server_context: ServerContext):
    coroutines: [Coroutine] = []
    for user in game.opponents.keys():
        viewers: Set[str] = server_context.viewer_set_by_viewed.get(user, set())
        for viewer in viewers:
            if viewer in server_context.connections_by_user:
                coroutine: Coroutine = send(viewer, server_context,
                                            json.dumps({"type": "update", "fen_board": game.board.fen()}))
                coroutines.append(coroutine)
    await asyncio.wait(coroutines)


async def handle_move_msg(server_context: ServerContext,
                          game: Game,
                          uci_move: str):
    # todo handle verification here
    game.board.push_uci(uci_move)
    outcome: chess.Outcome = game.board.outcome(claim_draw=True)
    is_game_over = outcome is not None
    is_white_to_move = game.board.turn
    winner_user = get_game_winner(game, outcome)

    viewer_update_coroutine: Coroutine = send_viewer_update(game=game, server_context=server_context)

    coroutines: [Coroutine] = [viewer_update_coroutine]
    for user in game.opponents.keys():
        is_white = game.first_user == user
        is_white_and_white_turn = (is_white and is_white_to_move)
        is_black_and_black_turn = (not is_white and not is_white_to_move)
        is_next_to_move = not is_game_over and (is_white_and_white_turn or is_black_and_black_turn)
        move_made_msg: str = json.dumps({"type": "move_made",
                                         "uci_move": uci_move,
                                         "fen_board": game.board.fen(),
                                         "is_next_to_move": is_next_to_move,
                                         "winner": winner_user,
                                         "outcome": str(outcome)})
        coroutines.append(send(user, server_context, move_made_msg))
    await asyncio.wait(coroutines)


def get_game_winner(game: Game, outcome: chess.Outcome):
    return get_winner(game.first_user, game.opponents.get(game.first_user), outcome)


def get_winner(user1: str, user2: str, outcome: chess.Outcome):
    if outcome is None:
        return None
    if outcome.winner is None:
        return None
    if outcome.winner is chess.WHITE:
        return user1
    return user2


async def handle_player_msg(websocket: WebSocketCommonProtocol,
                            server_context: ServerContext,
                            json_msg: dict):
    user: str = json_msg.get("user")
    # retrieve game, or register then queue or start new game
    new_game_opt: Optional[Game] = None
    async with server_context.user_mgmt_lock:
        user_opt: str = server_context.users_by_connection.get(websocket, None)
        existing_game_opt: Optional[Game] = server_context.games_by_user.get(user_opt, None)
        if existing_game_opt is None:
            new_game_opt = register(user=user, websocket=websocket, server_context=server_context)
    if new_game_opt is not None:
        await start_game(server_context, new_game_opt)
    if existing_game_opt is None:
        return
    msg_type: str = json_msg.get("type")
    if msg_type == "move":
        await handle_move_msg(server_context=server_context,
                              game=existing_game_opt,
                              uci_move=json_msg.get("uci_move"))


async def unregister(websocket: WebSocketCommonProtocol,
                     server_context: ServerContext):
    try:
        async with server_context.user_mgmt_lock:
            user: Optional[str] = server_context.users_by_connection.pop(websocket, None)
            server_context.connections_by_user.pop(user, None)
            game_opt: Optional[Game] = server_context.games_by_user.pop(user, None)

        if game_opt is not None:
            opponent: str = game_opt.opponents.get(user)
            opp_conn_opt: Optional[WebSocketCommonProtocol] = server_context.users_by_connection.get(opponent, None)
            if opp_conn_opt is not None:
                await send(user=opponent,
                           server_context=server_context,
                           message=json.dumps({"type": "opponent_left"}))
                await unregister(websocket=opp_conn_opt, server_context=server_context)
    except Exception:
        logger.exception(f"Failed to unregister websocket with address <{get_cli_address(websocket)}>")


async def on_connection(websocket: WebSocketCommonProtocol,
                        server_context: ServerContext,
                        path: str):
    logger.info(f"{get_cli_address(websocket)} Opening new websocket connection for path <{path}>")
    is_viewer = path == "/spectate"
    if is_viewer:
        await on_viewer_connection(server_context, websocket)
    else:
        await on_player_connection(server_context, websocket)


async def on_player_connection(server_context: ServerContext, websocket: WebSocketCommonProtocol):
    message_idx = 0
    last_message = None
    try:
        async for str_msg in websocket:
            session_id = get_cli_address(websocket)
            logger.info(f"{session_id} #{message_idx + 1} Received < {str_msg} >")
            last_message = str_msg
            json_msg: dict = json.loads(str_msg)
            await handle_player_msg(websocket=websocket, server_context=server_context, json_msg=json_msg)
            message_idx += 1
        logger.info(f"{get_cli_address(websocket)} Finished receiving messages")
    except ConnectionClosed:
        logger.error(f"{get_cli_address(websocket)} Connection closed")
    except:
        logger.exception(f"{get_cli_address(websocket)} An exception occurred while processing message "
                         f"< {last_message} >.",
                         exc_info=sys.exc_info())
        websocket.fail_connection()
    finally:
        await unregister(websocket, server_context)


async def handle_viewer_msg(websocket: WebSocketCommonProtocol,
                            server_context: ServerContext,
                            json_msg: dict):
    user: str = json_msg.get("user")
    viewed: str = json_msg.get("viewed")

    existing_user: Optional[str] = server_context.users_by_connection.get(websocket, None)
    existing_connection: Optional[WebSocketCommonProtocol] = server_context.connections_by_user.get(user, None)
    # todo check existing values are consistent with new ones, if there are any

    if existing_user is not None:
        return
    server_context.users_by_connection[websocket] = user
    server_context.connections_by_user[user] = websocket

    async with server_context.user_mgmt_lock:
        viewer_set: set = server_context.viewer_set_by_viewed.get(viewed, set())
        viewer_set.add(user)
        server_context.viewer_set_by_viewed[viewed] = viewer_set

    game_opt: Optional[Game] = server_context.games_by_user.get(viewed, None)
    if game_opt is None:
        return
    board: chess.Board = copy.deepcopy(game_opt.board)
    fen_boards: [str] = [board.fen()]
    move_stack_size: int = len(board.move_stack)
    for _ in range(move_stack_size):
        board.pop()
        fen_boards.append(board.fen())
    await send(user, server_context, json.dumps({"type": "bulk_update", "fen_boards": fen_boards}))


async def on_viewer_connection(server_context: ServerContext, websocket: WebSocketCommonProtocol):
    message_idx = 0
    last_message = None

    try:
        async for str_msg in websocket:
            session_id = get_cli_address(websocket)
            logger.info(f"{session_id} #{message_idx + 1} Received < {str_msg} >")
            json_msg: dict = json.loads(str_msg)
            await handle_viewer_msg(websocket=websocket, server_context=server_context, json_msg=json_msg)
    except ConnectionClosed:
        logger.error(f"{get_cli_address(websocket)} Connection closed")
    except:
        logger.exception(f"{get_cli_address(websocket)} An exception occurred while processing message "
                         f"< {last_message} >.",
                         exc_info=sys.exc_info())
        websocket.fail_connection()


def generate_game(user1, user2):
    opponents: Dict[str, str] = dict()
    opponents[user1] = user2
    opponents[user2] = user1
    first_user: str = user1
    return Game(opponents, first_user, chess.Board())


class WsServer(GenericServer):
    def __init__(self):
        self.context = ServerContext(dict(), dict(), None, dict(), dict(), asyncio.Lock(), dict())

    def start(self, host: str, port: int):
        logger.info(f"Starting websocket server as ws://{host}:{port}")

        async def ws_handler_fn(websocket, path):
            return await on_connection(websocket,
                                       self.context,
                                       path)

        start_server = serve(ws_handler_fn, host, port)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(start_server)
        loop.run_forever()
