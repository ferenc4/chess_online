import random

import chess


class Bot:
    def name(self):
        raise NotImplementedError

    def next_move(self, board: chess.Board) -> chess.Move:
        raise NotImplementedError


class RandomBot(Bot):
    def name(self):
        return "randomBot"

    def next_move(self, board: chess.Board) -> chess.Move:
        legal_moves = list(board.legal_moves)
        return legal_moves[random.randrange(0, len(legal_moves))]
